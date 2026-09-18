"""Terminal control: raw mode, pixel-aware geometry, deterministic key decoding.

Design notes (lessons that motivated this module):
- One reader owns stdin. Keys and graphics-protocol responses both arrive on
  fd 0; decoding through a single pushback buffer avoids the classic
  curses-buffer vs sys.stdin.buffer split that silently eats or duplicates
  bytes.
- Escape sequences are consumed whole. A lone ESC gets a short grace window;
  unknown sequences are swallowed instead of leaking into the key stream.
"""

import fcntl
import os
import select
import signal
import struct
import sys
import termios
import time

_ESC = 0x1B
_ESC_GRACE = 0.03  # seconds to wait for the rest of an escape sequence

_CSI_KEYS = {
    b"A": "up", b"B": "down", b"C": "right", b"D": "left",
    b"H": "home", b"F": "end",
    b"1~": "home", b"2~": "ignored", b"3~": "ignored", b"4~": "end",
    b"5~": "pgup", b"6~": "pgdn", b"7~": "home", b"8~": "end",
}


class Terminal:
    """Raw terminal with TIOCGWINSZ geometry and a key decoder."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self._attrs = None
        self._buf = b""
        self._eof = False
        self._winch = False
        self.rows = self.cols = 1
        self.xpixel = self.ypixel = 0
        self.cell_w = 0.0
        self.cell_h = 0.0

    # -- lifecycle ------------------------------------------------------

    def enter(self):
        self._attrs = termios.tcgetattr(self.fd)
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, self._raw_attrs())
        try:
            signal.signal(signal.SIGWINCH, self._on_winch)
        except ValueError:
            pass  # not the main thread; resizes will be picked up lazily
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        self.refresh_size()

    def exit(self):
        try:
            self.write("\x1b[?25h\x1b[?1049l")
        except Exception:
            pass
        if self._attrs is not None:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, self._attrs)

    def _raw_attrs(self):
        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        iflag &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                   | termios.ISTRIP | termios.IXON)
        oflag &= ~termios.OPOST
        cflag &= ~(termios.CSIZE | termios.PARENB)
        cflag |= termios.CS8
        lflag &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        cc = list(cc)
        cc[termios.VMIN] = 1
        cc[termios.VTIME] = 0
        return [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]

    def _on_winch(self, *_):
        self._winch = True

    # -- output ---------------------------------------------------------

    def write(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def write_bytes(self, data):
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    # -- geometry -------------------------------------------------------

    def refresh_size(self):
        buf = fcntl.ioctl(self.fd, termios.TIOCGWINSZ,
                          struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, xpixel, ypixel = struct.unpack("HHHH", buf)
        self.rows = max(rows, 1)
        self.cols = max(cols, 1)
        if xpixel and ypixel:
            self.xpixel = xpixel
            self.ypixel = ypixel
            self.cell_w = xpixel / self.cols
            self.cell_h = ypixel / self.rows

    @property
    def has_pixel_size(self):
        return bool(self.xpixel and self.ypixel and self.cell_w and self.cell_h)

    def take_resize(self):
        """True once per resize event; refreshes geometry each time."""
        if not self._winch:
            return False
        self._winch = False
        self.refresh_size()
        return True

    # -- input ----------------------------------------------------------

    def read_raw(self, size=4096):
        """Low-level read that first drains the pushback buffer."""
        if self._buf:
            data, self._buf = self._buf, b""
            return data
        return os.read(self.fd, size)

    def drain_typeahead(self):
        """Drop queued input (pushback buffer + kernel tty queue).

        Used when crossing mode boundaries (entering/leaving the ToC
        overlay): a held key would otherwise oscillate open/close/open
        from its repeat backlog long after release.
        """
        self._buf = b""
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def _read_more(self, timeout):
        if self._eof:
            return False
        if select.select([self.fd], [], [], timeout)[0]:
            chunk = os.read(self.fd, 4096)
            if chunk:
                self._buf += chunk
                return True
            self._eof = True  # ready fd + b"": stdin is closed (EOF)
        return False

    def read_key(self, timeout=None):
        """Decode one key event.

        Returns a (kind, value) tuple, or None on timeout. Kinds:
        'char' (printable), 'ctrl' (value 'A'..'Z'), 'key' (named:
        up/down/... /ignored), 'enter', 'backspace', 'esc', 'eof'
        (stdin closed; sticky — buffered keys are drained first).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._buf:
                key = self._decode()
                if key is not None:
                    return key
                if self._buf[0] == _ESC:
                    # incomplete sequence: give it a short grace window
                    grace = _ESC_GRACE
                    if deadline is not None:
                        grace = min(grace, max(0.0, deadline - time.monotonic()))
                    self._read_more(grace)
                    key = self._decode()
                    if key is not None:
                        return key
                    # nothing more arrived: lone ESC (or junk) — commit it
                    self._buf = self._buf[1:]
                    return ("esc", "")
                # non-ESC prefix that failed to decode should not happen
                self._buf = self._buf[1:]
                continue
            if self._eof:
                return ("eof", "")
            if deadline is not None:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return None
            else:
                wait = None
            self._read_more(wait)

    def _decode(self):
        buf = self._buf
        if not buf:
            return None
        b0 = buf[0]
        if b0 != _ESC:
            self._buf = buf[1:]
            if b0 in (0x0D, 0x0A):
                return ("enter", "\r")
            if b0 in (0x7F, 0x08):
                return ("backspace", "")
            if 0x20 <= b0 < 0x7F:
                return ("char", chr(b0))
            if b0 < 0x20:
                return ("ctrl", chr(b0 + 0x40))
            n = 2 if 0xC0 <= b0 < 0xE0 else 4 if b0 >= 0xF0 else 3
            if len(buf) < n:
                self._buf = buf  # need more bytes
                return None
            try:
                ch = buf[:n].decode("utf-8")
            except UnicodeDecodeError:
                ch = "?"
            self._buf = buf[n:]
            return ("char", ch)
        if len(buf) < 2:
            return None
        if buf[1] in (0x5B, 0x4F):  # CSI or SS3
            for i in range(2, len(buf)):
                if 0x40 <= buf[i] <= 0x7E:  # final byte
                    seq = bytes(buf[2:i + 1])
                    self._buf = buf[i + 1:]
                    return ("key", _CSI_KEYS.get(seq, "ignored"))
            return None  # incomplete sequence
        # ESC + non-introducer: treat as alt-modified key, decode the base
        self._buf = buf[1:]
        return self._decode()
