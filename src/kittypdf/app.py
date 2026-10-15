"""Application: main loop, key bindings, status bar, per-file progress cache."""

import argparse
import hashlib
import json
import os
import sys

from . import __version__, graphics
from .help import help_loop
from .render import Document
from .term import Terminal
from .toc import _dw, _fit, toc_loop

_IMAGE_BANKS = ((1, 2), (3, 4))  # alternate front/back pairs for atomic page turns
_GUTTER_CELLS = 1        # blank columns between the two pages of a spread
_MODES = ("auto", "single", "dual")

# Progress-cache eviction: keep at most this many state files, deleting the
# least recently modified ones first (a simple size-capped LRU).
_MAX_CACHE_ENTRIES = 128

# Colemak-flavoured bindings: e/u move down/up (the vertical half of the
# colemak movement diamond), E/U are its big-jump variants.  j/k/b remain as
# qwerty aliases — terminals deliver layout-translated bytes, so they cannot
# collide with the colemak keys.
_BIG_JUMP = 10
_STEP_KEYS = {
    "e": +1, "j": +1, " ": +1,
    "u": -1, "k": -1, "b": -1,
    "E": +_BIG_JUMP, "U": -_BIG_JUMP,
}


class Progress:
    """Remember reader state per file under ~/.cache/kittypdf/."""

    def __init__(self):
        base = os.environ.get("XDG_CACHE_HOME",
                              os.path.expanduser("~/.cache"))
        self.dir = os.path.join(base, "kittypdf")
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError:
            self.dir = None

    def _file(self, path):
        key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:16]
        return os.path.join(self.dir, f"{key}.json")

    def load(self, path):
        """Load saved reader state; never raises on corrupt caches."""
        state = {"page": 0, "invert": False, "crop": False,
                 "transparent": False}
        if not self.dir:
            return state
        try:
            with open(self._file(path)) as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return state
        if isinstance(saved, dict):
            try:
                page = int(saved.get("page", 0))
            except (TypeError, ValueError):
                page = 0
            state["page"] = max(page, 0)
            state["invert"] = bool(saved.get("invert", False))
            state["crop"] = bool(saved.get("crop", False))
            state["transparent"] = bool(saved.get("transparent", False))
        return state

    def save(self, path, page, invert=False, crop=False, transparent=False):
        if not self.dir:
            return
        try:
            with open(self._file(path), "w") as fh:
                json.dump({"page": page, "invert": invert, "crop": crop,
                           "transparent": transparent}, fh)
        except OSError:
            pass
        self._evict()

    def _evict(self):
        """Keep the cache bounded: delete least-recently-modified entries."""
        try:
            entries = []
            for name in os.listdir(self.dir):
                if not name.endswith(".json"):
                    continue
                full = os.path.join(self.dir, name)
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            excess = len(entries) - _MAX_CACHE_ENTRIES
            for _, full in sorted(entries)[:max(excess, 0)]:
                try:
                    os.unlink(full)
                except OSError:
                    pass
        except OSError:
            pass


class Reader:
    """Owns the page state and everything drawn on screen."""

    def __init__(self, doc, term):
        self.doc = doc
        self.term = term
        self.page = 0
        self.invert = False
        self.autocrop = False
        self.transparent = False
        self.mode = "auto"       # auto | single | dual
        self._sent = None        # signature of what is currently on screen
        self._spread = False     # whether the last draw was a two-page spread
        self._bank = 1           # first draw uploads into bank 0
        self._active = ()        # IDs owned by the currently displayed frame
        self._clear = False      # defer screen clears until the next frame is ready

    # -- layout ---------------------------------------------------------

    def _viewport(self):
        t = self.term
        return t.xpixel, int((t.rows - 1) * t.cell_h)

    def _want_spread(self, avail_w, avail_h):
        """Decide whether to show two pages side by side.

        auto: pick the layout that renders the pages larger — a spread wins
        only when the viewport is wide enough that a single page would just
        be limited by height and waste the sides.
        """
        if self.mode == "single":
            return False
        if self.doc.page_count < 2:
            return False
        pw, ph = self.doc.page_size(self.page, autocrop=self.autocrop)
        if self.mode == "dual":
            return True
        gutter = _GUTTER_CELLS * self.term.cell_w
        single = min(avail_w / pw, avail_h / ph)
        double = min((avail_w - gutter) / (2 * pw), avail_h / ph)
        # A spread wins whenever two pages fit without shrinking either below
        # the single-page size — i.e. when a single page is height-limited and
        # the freed horizontal space can hold the second page. When both are
        # height-limited the two sizes are equal, and a spread is pure win
        # (same size, twice the content), so accept equality.
        return double >= single - 1e-6

    def _pair(self):
        """(left, right) 0-based page numbers for the current spread."""
        left = self.page - (self.page % 2)   # spreads start on even pages
        right = left + 1 if left + 1 < self.doc.page_count else None
        return left, right

    # -- drawing --------------------------------------------------------

    def invalidate(self):
        """Request a fresh frame, deferring the clear until it is ready."""
        self._sent = None
        self._clear = True

    def draw(self, count=""):
        """Prepare off-screen, then swap page(s) and footer in one short update."""
        t = self.term
        avail_w, avail_h = self._viewport()
        spread = self._want_spread(avail_w, avail_h)
        left, right = self._pair() if spread else (self.page, None)
        sig = (spread, left, right, self.invert, self.autocrop,
               self.transparent, avail_w, avail_h, t.cell_w, t.cell_h)
        if sig == self._sent:
            self.status(count)
            return  # no duplicate placements (important for transparent pages)

        gutter = _GUTTER_CELLS * t.cell_w
        page_w = (avail_w - gutter) / 2 if spread else avail_w
        bank = 1 - self._bank
        frame = []
        try:
            # Finish BOTH renders before touching the terminal. A failed right
            # page must leave the entire old spread on screen.
            for slot, pno in enumerate((left, right)):
                if pno is None:
                    continue
                png, w, h = self.doc.render(pno, page_w, avail_h,
                                            invert=self.invert,
                                            autocrop=self.autocrop,
                                            transparent=self.transparent)
                x = (avail_w - w) / 2
                if spread:
                    x = page_w - w if slot == 0 else page_w + gutter
                row = int((avail_h - h) / 2 / t.cell_h) + 1
                col = max(1, int(x / t.cell_w) + 1)
                frame.append((_IMAGE_BANKS[bank][slot], png, row, col))
        except Exception as exc:  # noqa: BLE001 - report and stay alive
            self.status(msg=f"render error: {exc}")
            return

        # Transmit into inactive IDs: reusing a visible ID would delete the old
        # image immediately. Never hold synchronized output over slow uploads.
        for image_id, png, _, _ in frame:
            graphics.send_image(t, image_id, png)
        with graphics.synchronized(t):
            if self._clear:
                # ED2 (CSI 2J) frees even newly uploaded, unplaced images in
                # kitty. EL2 clears text only, preserving both image banks.
                t.write("\x1b[0m" + "".join(
                    f"\x1b[{row};1H\x1b[2K" for row in range(1, t.rows + 1)))
            for image_id, _, row, col in frame:
                graphics.place(t, image_id, row, col)
            for image_id in self._active:
                graphics.delete_image(t, image_id)
            self._spread = spread
            self.status(count)
        self._bank = bank
        self._active = tuple(image_id for image_id, _, _, _ in frame)
        self._sent = sig
        self._clear = False

    def status(self, count="", msg=""):
        t = self.term
        left = msg or (count if count else "kittypdf  ? help")
        flags = (("-" if self.invert else "")
                 + ("c" if self.autocrop else "")
                 + ("a" if self.transparent else ""))
        if self.mode != "auto":
            flags += self.mode[0]            # 's' or 'd' when forced
        if self._spread:
            lp, rp = self._pair()
            pages = f"{lp + 1}-{rp + 1}" if rp is not None else f"{lp + 1}"
        else:
            pages = f"{self.page + 1}"
        right = f"[{pages}/{self.doc.page_count}]{flags}"
        # Truncate the message by display width (CJK counts as 2 cells) so a
        # long error can never wrap onto a second line and scroll the screen.
        budget = t.cols - _dw(right) - 3
        shown = _fit(left, max(1, budget))
        pad = max(1, t.cols - _dw(shown) - _dw(right) - 2)
        t.write(f"\x1b[{t.rows};1H\x1b[2K {shown}{' ' * pad}{right} ")

    # -- navigation -----------------------------------------------------

    def clamp(self, page):
        return max(0, min(self.doc.page_count - 1, page))

    def _stride(self):
        """Pages moved per step: 2 in a spread, 1 otherwise."""
        return 2 if self._spread else 1

    def step(self, delta, count):
        self.page = self.clamp(self.page + delta * count * self._stride())
        if self._spread:
            self.page -= self.page % 2       # keep spreads aligned to pairs

    def goto(self, page, count=None):
        target = (count - 1) if count is not None else page
        self.page = self.clamp(target)

    def cycle_mode(self):
        self.mode = _MODES[(_MODES.index(self.mode) + 1) % len(_MODES)]


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kittypdf",
        description="Read a PDF or EPUB inside the kitty terminal.")
    parser.add_argument("file", help="path to a PDF or EPUB file")
    parser.add_argument("-p", "--page", type=int, default=None,
                        help="open at this page (1-based)")
    parser.add_argument("-m", "--mode", choices=_MODES, default="auto",
                        help="page layout: auto (default), single, or dual")
    transparency = parser.add_mutually_exclusive_group()
    transparency.add_argument("--transparent", dest="transparent",
                              action="store_true", default=None,
                              help="keep unpainted PDF paper transparent")
    transparency.add_argument("--no-transparent", dest="transparent",
                              action="store_false",
                              help="force an opaque PDF paper background")
    parser.add_argument("-V", "--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.file):
        print(f"kittypdf: no such file: {args.file}", file=sys.stderr)
        return 1
    try:
        doc = Document(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"kittypdf: cannot open {args.file}: {exc}", file=sys.stderr)
        return 1
    if doc.page_count == 0:
        print(f"kittypdf: {args.file} has no pages", file=sys.stderr)
        return 1

    # Read the cached state before entering raw mode so a corrupt cache can
    # never leave the terminal stuck halfway through setup.
    progress = Progress()
    state = progress.load(args.file)

    term = Terminal()
    error = None
    try:
        # enter() inside the try: even a crash partway through setup must
        # restore the terminal in the finally below (TERM-3).
        term.enter()
        if not graphics.probe(term):
            error = 2, ("terminal does not support the kitty graphics "
                        "protocol")
        elif not term.has_pixel_size:
            error = 2, ("terminal does not report pixel cell sizes "
                        "(kitty does)")
        else:
            reader = Reader(doc, term)
            reader.invert = state["invert"]
            reader.autocrop = state["crop"]
            reader.transparent = (state["transparent"]
                                  if args.transparent is None
                                  else args.transparent)
            reader.mode = args.mode
            start = state["page"]
            if args.page is not None:
                start = args.page - 1
            reader.page = reader.clamp(start)
            _loop(reader, doc, term, args.file, progress)
            return 0
    finally:
        try:
            graphics.delete_all(term)
            # Also free inactive uploads if an exception interrupted the swap.
            for bank in _IMAGE_BANKS:
                for image_id in bank:
                    graphics.delete_image(term, image_id)
        except Exception:
            pass
        term.exit()
    # Report setup failures only after the alternate screen was popped and
    # cooked mode restored, so the message lands on the real screen instead
    # of being wiped with the buffer (GFX-5/APP-1).
    if error:
        code, msg = error
        print(f"kittypdf: {msg}", file=sys.stderr)
        return code


def _loop(reader, doc, term, path, progress):
    count = ""
    pending_g = False

    reader.invalidate()
    reader.draw()

    def checkpoint():
        progress.save(path, reader.page, reader.invert, reader.autocrop,
                      reader.transparent)

    def redraw():
        reader.invalidate()
        reader.draw()

    while True:
        if term.take_resize():
            reader.invalidate()
            reader.draw(count)

        event = term.read_key(timeout=0.25)
        if event is None:
            continue
        kind, val = event
        if kind == "eof":  # stdin closed (pty hung up): quit the normal way
            checkpoint()
            return
        if kind == "key" and val == "ignored":
            continue  # unknown sequences never touch the count buffer

        n = int(count) if count else 1

        if kind == "char":
            if val.isdigit():
                count += val
                reader.status(count)
                continue
            if val in _STEP_KEYS:
                reader.step(_STEP_KEYS[val], n)
            elif val == "g":
                if pending_g:
                    reader.goto(0, n if count else None)
                else:
                    pending_g = True
                    reader.status("g" + count)
                    continue
            elif val == "G":
                reader.goto(doc.page_count - 1, n if count else None)
            elif val == "i":
                reader.invert = not reader.invert
            elif val == "a":
                reader.transparent = not reader.transparent
            elif val == "c":
                reader.autocrop = not reader.autocrop
            elif val == "d":
                reader.cycle_mode()
                reader.invalidate()   # layout change: wipe and re-lay-out
            elif val == "t":
                term.drain_typeahead()  # drop repeat backlog before entering
                action, value = toc_loop(term, doc.toc, reader.page, redraw=redraw)
                term.drain_typeahead()  # drop queued repeats on the way out too
                if action == "eof":
                    checkpoint()
                    return
                if action == "jump":
                    reader.page = reader.clamp(value)
                # Erase the overlay cells and re-transmit the page, including
                # when t/q/Esc returns without changing the current page.
                reader.invalidate()
            elif val == "?":
                term.drain_typeahead()
                action = help_loop(term)
                term.drain_typeahead()
                if action == "eof":
                    checkpoint()
                    return
                reader.invalidate()
            elif val in ("r", "R"):
                reader.invalidate()
            elif val in ("q", "Q"):
                checkpoint()
                return
            else:
                count = ""  # any unrelated key clears the pending count
                pending_g = False
                continue
        elif kind == "key":
            if val in ("down", "pgdn"):
                reader.step(+1, n)
            elif val in ("up", "pgup"):
                reader.step(-1, n)
            elif val == "home":
                reader.goto(0)
            elif val == "end":
                reader.goto(doc.page_count - 1)
            else:
                count = ""
                pending_g = False
                continue
        elif kind == "enter":
            reader.step(+1, n)
        elif kind == "ctrl" and val in ("C", "L"):
            if val == "C":
                checkpoint()
                return
            reader.invalidate()
        else:
            count = ""
            pending_g = False
            continue

        count = ""
        pending_g = False
        reader.draw()
        checkpoint()


if __name__ == "__main__":
    sys.exit(main())
