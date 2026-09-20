"""Kitty graphics protocol, minimal and quiet.

Every fire-and-forget command carries q=2 ("do not reply, even on error"),
so the terminal never emits asynchronous ACKs or error responses that could
pollute the key stream, and we never block waiting for a reply. The only
response we read is the one-time support probe (a=q always replies).
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
    found_frame = False
    while True:
        start = data.find(_APC_START, pos)
        if start < 0:
            return False
        end = data.find(_APC_END, start + len(_APC_START))
        if end < 0:
            return False
        pos = end + len(_APC_END)
        found_frame = True
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
    first = True
    while data:
        chunk, data = data[:_CHUNK], data[_CHUNK:]
        cmd = {"q": 2, "m": 1 if data else 0}
        if first:
            cmd.update({"a": "t", "i": image_id, "f": 100})
            first = False
        term.write_bytes(_apc(cmd, chunk))


def place(term, image_id, row, col):
    """Place an image without moving the text cursor or scrolling the screen."""
    term.write(f"\x1b[{row};{col}H")
    term.write_bytes(_apc({"a": "p", "i": image_id, "z": -1, "C": 1,
                           "q": 2}))


def delete_image(term, image_id):
    """Delete one image entirely (d=i frees data + placements).

    kitty's plain a=d,i=<id> only removes the image's visible placements
    while keeping the transmitted bitmap in memory; d=i additionally frees
    the stored data, which is what page-turn invalidation wants (the id is
    re-transmitted for the next draw anyway).
    """
    term.write_bytes(_apc({"a": "d", "d": "i", "i": image_id, "q": 2}))


def delete_all(term):
    term.write_bytes(_apc({"a": "d", "d": "a", "q": 2}))
