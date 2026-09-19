"""Application: main loop, key bindings, status bar, per-file progress cache."""

import argparse
import hashlib
import json
import os
import sys

from . import __version__, graphics
from .render import Document
from .term import Terminal
from .toc import toc_loop

_IMAGE_ID = 1
_IMAGE_ID_R = 2          # right page in dual-page (spread) mode
_GUTTER_CELLS = 1        # blank columns between the two pages of a spread
_MODES = ("auto", "single", "dual")

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
            state["page"] = int(saved.get("page", 0))
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
        self._size = (1, 1)      # single-page image pixel size
        self._spread_imgs = [None, None]  # cached (png, w, h) for the pair

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
        """Force a re-render (and wipe leftovers) on resize/refresh."""
        self._sent = None
        graphics.delete_all(self.term)
        self.term.write("\x1b[2J")

    def draw(self):
        t = self.term
        avail_w, avail_h = self._viewport()
        spread = self._want_spread(avail_w, avail_h)
        self._spread = spread
        if spread:
            self._draw_spread(avail_w, avail_h)
        else:
            self._draw_single(avail_w, avail_h)

    def _draw_single(self, avail_w, avail_h):
        t = self.term
        sig = ("single", self.page, self.invert, self.autocrop,
               self.transparent, avail_w, avail_h)
        if sig != self._sent:
            try:
                png, w, h = self.doc.render(self.page, avail_w, avail_h,
                                            invert=self.invert,
                                            autocrop=self.autocrop,
                                            transparent=self.transparent)
            except Exception as exc:  # noqa: BLE001 - report and stay alive
                self.status(msg=f"render error: {exc}")
                return
            graphics.delete_image(t, _IMAGE_ID_R)
            graphics.delete_image(t, _IMAGE_ID)
            graphics.send_image(t, _IMAGE_ID, png)
            self._sent = sig
            self._size = (w, h)
        w, h = self._size
        col = int((avail_w - w) / 2 / t.cell_w) + 1
        row = int((avail_h - h) / 2 / t.cell_h) + 1
        graphics.place(t, _IMAGE_ID, row, col)

    def _draw_spread(self, avail_w, avail_h):
        t = self.term
        left, right = self._pair()
        sig = ("dual", left, right, self.invert, self.autocrop,
               self.transparent, avail_w, avail_h)
        gutter = _GUTTER_CELLS * t.cell_w
        half = (avail_w - gutter) / 2
        if sig != self._sent:
            try:
                imgs = []
                for pno in (left, right):
                    if pno is None:
                        imgs.append(None)
                        continue
                    png, w, h = self.doc.render(pno, half, avail_h,
                                                invert=self.invert,
                                                autocrop=self.autocrop,
                                                transparent=self.transparent)
                    imgs.append((png, w, h))
            except Exception as exc:  # noqa: BLE001
                self.status(msg=f"render error: {exc}")
                return
            graphics.delete_image(t, _IMAGE_ID)
            graphics.delete_image(t, _IMAGE_ID_R)
            if imgs[0]:
                graphics.send_image(t, _IMAGE_ID, imgs[0][0])
            if imgs[1]:
                graphics.send_image(t, _IMAGE_ID_R, imgs[1][0])
            self._sent = sig
            self._spread_imgs = imgs
        imgs = self._spread_imgs
        # left page: right-aligned against the gutter; right page: left-aligned
        if imgs[0]:
            _, w, h = imgs[0]
            x = half - w                      # hug the centre gutter
            row = int((avail_h - h) / 2 / t.cell_h) + 1
            col = int(x / t.cell_w) + 1
            graphics.place(t, _IMAGE_ID, row, max(col, 1))
        if imgs[1]:
            _, w, h = imgs[1]
            x = half + gutter                 # start just past the gutter
            row = int((avail_h - h) / 2 / t.cell_h) + 1
            col = int(x / t.cell_w) + 1
            graphics.place(t, _IMAGE_ID_R, row, col)

    def status(self, count="", msg=""):
        t = self.term
        left = msg or (count if count else "kittypdf")
        flags = (("-" if self.invert else "")
                 + ("c" if self.autocrop else "")
                 + ("a" if self.transparent else ""))
        if self.mode != "auto":
            flags += self.mode[0]            # 's' or 'd' when forced
        if self._spread:
            lp, rp = self._pair()
            if rp is not None:
                pages = f"{lp + 1}-{rp + 1}"
            else:
                pages = f"{lp + 1}"
        else:
            pages = f"{self.page + 1}"
        right = f"[{pages}/{self.doc.page_count}]{flags}"
        pad = max(1, t.cols - len(left) - len(right) - 2)
        t.write(f"\x1b[{t.rows};1H\x1b[2K {left}{' ' * pad}{right} ")

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
        description="Read a PDF inside the kitty terminal.")
    parser.add_argument("file", help="path to a PDF file")
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

    term = Terminal()
    term.enter()
    try:
        if not graphics.probe(term):
            print("kittypdf: terminal does not support the kitty graphics "
                  "protocol", file=sys.stderr)
            return 2
        if not term.has_pixel_size:
            print("kittypdf: terminal does not report pixel cell sizes "
                  "(kitty does)", file=sys.stderr)
            return 2

        progress = Progress()
        state = progress.load(args.file)
        reader = Reader(doc, term)
        reader.invert = state["invert"]
        reader.autocrop = state["crop"]
        reader.transparent = (state["transparent"] if args.transparent is None
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
        except Exception:
            pass
        term.exit()


def _loop(reader, doc, term, path, progress):
    count = ""
    pending_g = False

    reader.invalidate()
    reader.draw()
    reader.status()

    def checkpoint():
        progress.save(path, reader.page, reader.invert, reader.autocrop,
                      reader.transparent)

    while True:
        if term.take_resize():
            reader.invalidate()
            reader.draw()
            reader.status(count)

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
        moved = False

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
                moved = True
            elif val == "a":
                reader.transparent = not reader.transparent
                moved = True
            elif val == "c":
                reader.autocrop = not reader.autocrop
                moved = True
            elif val == "d":
                reader.cycle_mode()
                reader.invalidate()   # layout change: wipe and re-lay-out
                moved = True
            elif val == "t":
                term.drain_typeahead()  # drop repeat backlog before entering
                action, value = toc_loop(term, doc.toc, reader.page)
                term.drain_typeahead()  # and on the way out, so a held key
                                        # cannot oscillate the overlay
                if action == "eof":
                    checkpoint()
                    return
                if action == "jump":
                    reader.page = reader.clamp(value)
                # The overlay cleared the screen; force a full re-transmit so
                # the page image reappears. A place-only restore (same page,
                # unchanged signature) does NOT bring the image back once the
                # overlay's erase has dropped the placement -- which is why
                # cancel (q/Esc) looked stuck on the ToC while Enter, which
                # changes the page and re-transmits, worked.
                reader.invalidate()
                moved = True
            elif val in ("r", "R"):
                reader.invalidate()
                moved = True
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
            moved = True
        else:
            count = ""
            pending_g = False
            continue

        count = ""
        pending_g = False
        reader.draw()
        reader.status()
        checkpoint()


if __name__ == "__main__":
    sys.exit(main())
