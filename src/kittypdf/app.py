"""Application: main loop, key bindings, status bar, per-file progress cache."""

import argparse
import hashlib
import json
import os
import sys

from . import __version__, graphics
from .render import Document
from .term import Terminal

_IMAGE_ID = 1


class Progress:
    """Remember the last-read page per file under ~/.cache/kittypdf/."""

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

    def load(self, path, default=0):
        if not self.dir:
            return default
        try:
            with open(self._file(path)) as fh:
                return int(json.load(fh).get("page", default))
        except (OSError, ValueError):
            return default

    def save(self, path, page):
        if not self.dir:
            return
        try:
            with open(self._file(path), "w") as fh:
                json.dump({"page": page}, fh)
        except OSError:
            pass


class Reader:
    """Owns the page state and everything drawn on screen."""

    def __init__(self, doc, term):
        self.doc = doc
        self.term = term
        self.page = 0
        self.invert = False
        self._sent = None        # signature of the transmitted image
        self._size = (1, 1)      # pixel size of the transmitted image

    # -- drawing --------------------------------------------------------

    def _viewport(self):
        t = self.term
        return t.xpixel, int((t.rows - 1) * t.cell_h)

    def invalidate(self):
        """Force a re-render (and wipe leftovers) on resize/refresh."""
        self._sent = None
        graphics.delete_all(self.term)
        self.term.write("\x1b[2J")

    def draw(self):
        t = self.term
        avail_w, avail_h = self._viewport()
        sig = (self.page, self.invert, avail_w, avail_h)
        if sig != self._sent:
            try:
                png, w, h = self.doc.render(self.page, avail_w, avail_h,
                                            invert=self.invert)
            except Exception as exc:  # noqa: BLE001 - report and stay alive
                self.status(f"render error: {exc}")
                return
            graphics.delete_image(t, _IMAGE_ID)
            graphics.send_image(t, _IMAGE_ID, png)
            self._sent = sig
            self._size = (w, h)
        w, h = self._size
        col = int((avail_w - w) / 2 / t.cell_w) + 1
        row = int((avail_h - h) / 2 / t.cell_h) + 1
        graphics.place(t, _IMAGE_ID, row, col)

    def status(self, count="", msg=""):
        t = self.term
        left = msg or (count if count else "kittypdf")
        flags = "-" if self.invert else ""
        right = f"[{self.page + 1}/{self.doc.page_count}]{flags}"
        pad = max(1, t.cols - len(left) - len(right) - 2)
        t.write(f"\x1b[{t.rows};1H\x1b[2K {left}{' ' * pad}{right} ")

    # -- navigation -----------------------------------------------------

    def clamp(self, page):
        return max(0, min(self.doc.page_count - 1, page))

    def step(self, delta, count):
        self.page = self.clamp(self.page + delta * count)

    def goto(self, page, count=None):
        if count is not None:
            self.page = self.clamp(count - 1)
        else:
            self.page = self.clamp(page)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="kittypdf",
        description="Read a PDF inside the kitty terminal.")
    parser.add_argument("file", help="path to a PDF file")
    parser.add_argument("-p", "--page", type=int, default=None,
                        help="open at this page (1-based)")
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
        reader = Reader(doc, term)
        start = progress.load(args.file)
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

    while True:
        if term.take_resize():
            reader.invalidate()
            reader.draw()
            reader.status(count)

        event = term.read_key(timeout=0.25)
        if event is None:
            continue
        kind, val = event
        if kind == "key" and val == "ignored":
            continue  # unknown sequences never touch the count buffer

        n = int(count) if count else 1
        moved = False

        if kind == "char":
            if val.isdigit():
                count += val
                reader.status(count)
                continue
            if val == "j":
                reader.step(+1, n)
            elif val in ("k", "b"):
                reader.step(-1, n)
            elif val == " ":
                reader.step(+1, n)
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
            elif val in ("r", "R"):
                reader.invalidate()
                moved = True
            elif val == "q":
                progress.save(path, reader.page)
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
                progress.save(path, reader.page)
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
        progress.save(path, reader.page)


if __name__ == "__main__":
    sys.exit(main())
