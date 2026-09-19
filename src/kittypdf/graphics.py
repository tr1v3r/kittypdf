"""Kitty graphics protocol, minimal and quiet.

Every fire-and-forget command carries q=1 ("do not reply"), so the terminal
never emits asynchronous ACKs that could pollute the key stream, and we never
block waiting for a response. The only response we read is the one-time
support probe (a=q always replies).
"""

import base64
import select
import time

_APC_START = b"\x1b_G"
_APC_END = b"\x1b\\"
_CHUNK = 4096


def _apc(cmd, payload=b""):
    head = ",".join(f"{k}={v}" for k, v in cmd.items()).encode("ascii")
    out = _APC_START + head
    if payload:
        out += b";" + payload
    return out + _APC_END


def probe(term, timeout=1.0):
    """True if the terminal speaks the kitty graphics protocol.

    The query must carry a minimal valid payload — RGB 1x1 is 3 bytes —
    because a=q runs the full load validation: an empty payload earns
    ENODATA instead of OK.  a=q never stores the image (spec), so there is
    no probe image to clean up afterwards.
    """
    term.write_bytes(_apc({"a": "q", "f": 24, "s": 1, "v": 1, "i": 1},
                          b"AAAA"))
    resp = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not resp.endswith(_APC_END):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([term.fd], [], [], remaining)[0]:
            break
        try:
            chunk = term.read_raw()
        except OSError:
            break
        if not chunk:
            break
        resp += chunk
    return b"OK" in bytes(resp)


def send_image(term, image_id, png_bytes):
    """Transmit a PNG (base64, chunked) under `image_id`, quietly.

    The payload is not deflated again: PNG is already compressed, and
    f=100 with o=z requires the S key per spec — without it the terminal
    rejects the data.  Plain f=100 avoids both problems.
    """
    data = base64.standard_b64encode(png_bytes)
    first = True
    while data:
        chunk, data = data[:_CHUNK], data[_CHUNK:]
        cmd = {"q": 1, "m": 1 if data else 0}
        if first:
            cmd.update({"a": "t", "i": image_id, "f": 100})
            first = False
        term.write_bytes(_apc(cmd, chunk))


def place(term, image_id, row, col):
    """Place an image without moving the text cursor or scrolling the screen."""
    term.write(f"\x1b[{row};{col}H")
    term.write_bytes(_apc({"a": "p", "i": image_id, "z": -1, "C": 1,
                           "q": 1}))


def delete_image(term, image_id):
    term.write_bytes(_apc({"a": "d", "i": image_id, "q": 1}))


def delete_all(term):
    term.write_bytes(_apc({"a": "d", "d": "a", "q": 1}))
