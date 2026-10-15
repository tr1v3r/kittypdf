"""Kitty graphics protocol, minimal and quiet.

Every fire-and-forget command carries q=2 ("do not reply, even on error"),
so the terminal never emits asynchronous ACKs or error responses that could
pollute the key stream, and we never block waiting for a reply. The only
response we read is the one-time support probe (a=q always replies).
"""

import base64
import select
import time
from contextlib import contextmanager

_APC_START = b"\x1b_G"
_APC_END = b"\x1b\\"
_CHUNK = 4096
_WRITE_BATCH = 64 * 1024  # amortize flushes without buffering an entire large PNG
# Kitty draws images with z < INT32_MIN/2 beneath cells with a custom
# background. The ToC card needs this so its background hides the PDF while
# the page remains visible around it.
_PAGE_Z = -1073741825


def _apc(cmd, payload=b""):
    head = ",".join(f"{k}={v}" for k, v in cmd.items()).encode("ascii")
    out = _APC_START + head
    if payload:
        out += b";" + payload
    return out + _APC_END


def _parse_probe_response(resp):
    """Strictly validate an a=q reply (GFX-6).

    Scan the buffer for complete APC frames (ESC _ G <fields> ESC \\) and
    accept only a well-formed one: semicolon-separated fields that are
    either KEY=VALUE pairs or the final OK/ERR status token, ending in OK.
    Bytes outside frames (e.g. keys typed during the probe window) are
    ignored — but a bare substring match would have accepted garbage that
    merely contains the bytes "OK", or a truncated frame.
    """
    data = bytes(resp)
    pos = 0
    while True:
        start = data.find(_APC_START, pos)
        if start < 0:
            return False
        end = data.find(_APC_END, start + len(_APC_START))
        if end < 0:
            return False
        pos = end + len(_APC_END)
        body = data[start + len(_APC_START):end]
        if not body:
            continue
        fields = body.split(b";")
        status = fields[-1]
        if status not in (b"OK", b"ERR"):
            continue
        well_formed = all(
            field == b""  # empty control-key section before the ';'
            or (b"=" in field and field.partition(b"=")[0]
                and field.partition(b"=")[2])
            for field in fields[:-1])
        if well_formed and status == b"OK":
            return True


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
        # Drain any pushbacked bytes before selecting on the fd: data that
        # already sits in the userspace buffer never makes the fd readable,
        # so without this the probe would sleep out its whole timeout (and
        # keys typed during the probe window would linger in the buffer).
        if not term.pending():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([term.fd], [], [],
                                                   remaining)[0]:
                break
        try:
            chunk = term.read_raw()
        except OSError:
            break
        if not chunk:
            break
        resp += chunk
    return _parse_probe_response(resp)


def send_image(term, image_id, png_bytes):
    """Transmit a PNG (base64, chunked) under `image_id`, quietly.

    The payload is not deflated again: PNG is already compressed, and
    f=100 with o=z requires the S key per spec — without it the terminal
    rejects the data.  Plain f=100 avoids both problems.
    """
    data = base64.standard_b64encode(png_bytes)
    batch = bytearray()
    for offset in range(0, len(data), _CHUNK):
        end = offset + _CHUNK
        cmd = {"q": 2, "m": int(end < len(data))}
        if offset == 0:
            cmd.update({"a": "t", "i": image_id, "f": 100})
        # Slice only this chunk, not the entire remaining payload (quadratic
        # copying on scan-heavy pages). Each APC payload stays <= 4096 bytes.
        batch.extend(_apc(cmd, data[offset:end]))
        if len(batch) >= _WRITE_BATCH:
            term.write_bytes(bytes(batch))
            batch.clear()
    if batch:
        term.write_bytes(bytes(batch))


@contextmanager
def synchronized(term):
    """Commit a short visual update as one frame on supporting terminals.

    Render and upload BEFORE entering: a slow PNG must not hold synchronized
    output open long enough for the terminal's safety timeout to expire.
    """
    term.write("\x1b[?2026h")
    try:
        yield
    finally:
        term.write("\x1b[?2026l")


def place(term, image_id, row, col):
    """Place an image without moving the text cursor or scrolling the screen."""
    term.write(f"\x1b[{row};{col}H")
    term.write_bytes(_apc({"a": "p", "i": image_id, "p": 1, "z": _PAGE_Z, "C": 1,
                           "q": 2}))


def delete_image(term, image_id):
    """Free one image and its placements (uppercase I also releases data).

    Lowercase d=i removes only placements; also release retired bitmaps rather
    than keeping them in terminal memory until their IDs are reused.
    """
    term.write_bytes(_apc({"a": "d", "d": "I", "i": image_id, "q": 2}))


def delete_all(term):
    term.write_bytes(_apc({"a": "d", "d": "a", "q": 2}))
