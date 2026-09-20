import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

import pymupdf

from kittypdf import app, graphics
from kittypdf.app import Progress, Reader
from kittypdf.render import Document, _inverted


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data):
    """Return width, height, channels and unfiltered 8-bit PNG samples."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    compressed = bytearray()
    width = height = color_type = bit_depth = interlace = None
    while pos < len(data):
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + size]
        pos += 12 + size
        if kind == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
        raise AssertionError((bit_depth, color_type, interlace))
    channels = 3 if color_type == 2 else 4
    packed = zlib.decompress(compressed)
    stride = width * channels
    previous = bytearray(stride)
    output = bytearray()
    cursor = 0
    for _ in range(height):
        filter_type = packed[cursor]
        cursor += 1
        scan = bytearray(packed[cursor:cursor + stride])
        cursor += stride
        for i in range(stride):
            left = scan[i - channels] if i >= channels else 0
            up = previous[i]
            upper_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                scan[i] = (scan[i] + left) & 0xFF
            elif filter_type == 2:
                scan[i] = (scan[i] + up) & 0xFF
            elif filter_type == 3:
                scan[i] = (scan[i] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                scan[i] = (scan[i] + _paeth(left, up, upper_left)) & 0xFF
            elif filter_type != 0:
                raise AssertionError(f"unsupported PNG filter {filter_type}")
        output.extend(scan)
        previous = scan
    return width, height, channels, bytes(output)


def pixel(decoded, x, y):
    width, _, channels, samples = decoded
    start = (y * width + x) * channels
    return tuple(samples[start:start + channels])


def make_pdf(path, paint=None):
    doc = pymupdf.open()
    page = doc.new_page(width=100, height=100)
    if paint:
        paint(page)
    doc.save(path)
    doc.close()


class RenderTransparencyTests(unittest.TestCase):
    def test_unpainted_paper_is_transparent_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plain.pdf")
            make_pdf(path, lambda page: page.insert_text((35, 55), "text"))
            doc = Document(path)
            transparent = decode_png(doc.render(0, 100, 100,
                                                transparent=True)[0])
            opaque = decode_png(doc.render(0, 100, 100)[0])
        self.assertEqual(transparent[2], 4)
        self.assertEqual(pixel(transparent, 0, 0)[3], 0)
        self.assertEqual(opaque[2], 3)
        self.assertEqual(pixel(opaque, 0, 0), (255, 255, 255))

    def test_explicit_white_background_stays_opaque(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "white.pdf")
            make_pdf(path, lambda page: page.draw_rect(
                page.rect, color=None, fill=(1, 1, 1)))
            decoded = decode_png(Document(path).render(
                0, 100, 100, transparent=True)[0])
        self.assertEqual(pixel(decoded, 50, 50), (255, 255, 255, 255))

    def test_full_page_raster_stays_opaque(self):
        def add_scan(page):
            pix = pymupdf.Pixmap(pymupdf.csRGB, 2, 2, bytes([
                230, 230, 230, 210, 210, 210,
                190, 190, 190, 170, 170, 170,
            ]), False)
            page.insert_image(page.rect, pixmap=pix)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scan.pdf")
            make_pdf(path, add_scan)
            decoded = decode_png(Document(path).render(
                0, 100, 100, transparent=True)[0])
        self.assertEqual(pixel(decoded, 50, 50)[3], 255)

    def test_transparent_invert_keeps_unpainted_paper_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "inverted.pdf")
            make_pdf(path, lambda page: page.insert_text((35, 55), "text"))
            decoded = decode_png(Document(path).render(
                0, 100, 100, transparent=True, invert=True)[0])
        self.assertEqual(pixel(decoded, 0, 0), (0, 0, 0, 0))

    def test_invert_preserves_premultiplied_alpha(self):
        source = pymupdf.Pixmap(pymupdf.csRGB, 1, 1,
                                bytes((20, 40, 60, 100)), True)
        inverted = _inverted(source)
        self.assertEqual(tuple(inverted.samples), (80, 60, 40, 100))

    def test_opaque_invert_keeps_existing_behavior(self):
        source = pymupdf.Pixmap(pymupdf.csRGB, 1, 1,
                                bytes((20, 40, 60)), False)
        inverted = _inverted(source)
        self.assertEqual(tuple(inverted.samples), (235, 215, 195))


class GraphicsTests(unittest.TestCase):
    def test_send_image_preserves_rgba_png_payload(self):
        class Sink:
            def __init__(self):
                self.data = bytearray()

            def write_bytes(self, data):
                self.data.extend(data)

        png = b"\x89PNG\r\n\x1a\nRGBA-alpha-payload"
        sink = Sink()
        graphics.send_image(sink, 9, png)
        raw = bytes(sink.data)
        chunks = raw.split(b"\x1b_G")[1:]
        payload = b"".join(chunk.split(b";", 1)[1].split(b"\x1b\\", 1)[0]
                           for chunk in chunks)
        self.assertEqual(base64.b64decode(payload), png)
        self.assertIn(b"f=100", raw)
        self.assertNotIn(b"o=z", raw)

    def test_place_does_not_move_cursor_or_scroll_footer(self):
        class Sink:
            def __init__(self):
                self.text = []
                self.data = bytearray()

            def write(self, value):
                self.text.append(value)

            def write_bytes(self, data):
                self.data.extend(data)

        sink = Sink()
        graphics.place(sink, 9, 3, 4)

        self.assertEqual(sink.text, ["\x1b[3;4H"])
        self.assertEqual(bytes(sink.data),
                         b"\x1b_Ga=p,i=9,z=-1,C=1,q=2\x1b\\")


class FakeTerm:
    xpixel = 800
    rows = 31
    cell_h = 20
    cell_w = 10
    cols = 80

    def __init__(self, events=None):
        self.events = list(events or [])
        self.output = []
        self.has_pixel_size = True

    def write(self, value):
        self.output.append(value)

    def take_resize(self):
        return False

    def read_key(self, timeout=None):
        return self.events.pop(0) if self.events else ("eof", "")

    def drain_typeahead(self):
        pass

    def enter(self):
        pass

    def exit(self):
        pass


class FakeDoc:
    def __init__(self, pages=1):
        self.page_count = pages
        self.toc = []
        self.calls = []

    def page_size(self, page, autocrop=False):
        return 100, 140

    def render(self, page, width, height, **options):
        self.calls.append((page, width, height, options))
        return b"png", 100, 140


class StateTests(unittest.TestCase):
    def test_old_progress_cache_defaults_to_opaque_and_new_value_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": tmp}):
            progress = Progress()
            path = "/tmp/book.pdf"
            with open(progress._file(path), "w") as fh:
                json.dump({"page": 7, "invert": True, "crop": True}, fh)
            self.assertFalse(progress.load(path)["transparent"])
            progress.save(path, 8, True, True, True)
            self.assertEqual(progress.load(path), {
                "page": 8, "invert": True, "crop": True,
                "transparent": True,
            })

    @mock.patch("kittypdf.app.graphics.place")
    @mock.patch("kittypdf.app.graphics.send_image")
    @mock.patch("kittypdf.app.graphics.delete_image")
    def test_transparency_invalidates_single_page_render(
            self, _delete, send, _place):
        doc = FakeDoc()
        reader = Reader(doc, FakeTerm())
        reader.draw()
        reader.transparent = True
        reader.draw()
        self.assertEqual([call[3]["transparent"] for call in doc.calls],
                         [False, True])
        self.assertEqual(send.call_count, 2)

    @mock.patch("kittypdf.app.graphics.place")
    @mock.patch("kittypdf.app.graphics.send_image")
    @mock.patch("kittypdf.app.graphics.delete_image")
    def test_spread_passes_transparency_to_both_pages(
            self, _delete, _send, _place):
        doc = FakeDoc(2)
        reader = Reader(doc, FakeTerm())
        reader.mode = "dual"
        reader.transparent = True
        reader.draw()
        self.assertEqual([call[3]["transparent"] for call in doc.calls],
                         [True, True])

    @mock.patch("kittypdf.app.graphics.place")
    @mock.patch("kittypdf.app.graphics.send_image")
    @mock.patch("kittypdf.app.graphics.delete_image")
    @mock.patch("kittypdf.app.graphics.delete_all")
    def test_a_key_toggles_and_checkpoints_transparency(
            self, _all, _delete, _send, _place):
        reader = Reader(FakeDoc(), FakeTerm([
            ("char", "a"), ("char", "q"),
        ]))
        progress = mock.Mock()
        app._loop(reader, reader.doc, reader.term, "/tmp/book.pdf", progress)
        self.assertTrue(reader.transparent)
        self.assertEqual(progress.save.call_args.args[-1], True)
        self.assertIn("a", "".join(reader.term.output))


class CliTests(unittest.TestCase):
    def _run(self, option, cached):
        doc = FakeDoc()
        term = FakeTerm()
        progress = mock.Mock()
        progress.load.return_value = {
            "page": 0, "invert": False, "crop": False,
            "transparent": cached,
        }
        argv = ([] if option is None else [option]) + ["book.pdf"]
        with mock.patch("kittypdf.app.os.path.isfile", return_value=True), \
                mock.patch("kittypdf.app.Document", return_value=doc), \
                mock.patch("kittypdf.app.Terminal", return_value=term), \
                mock.patch("kittypdf.app.Progress", return_value=progress), \
                mock.patch("kittypdf.app.graphics.probe", return_value=True), \
                mock.patch("kittypdf.app.graphics.delete_all"), \
                mock.patch("kittypdf.app._loop") as loop:
            self.assertEqual(app.main(argv), 0)
        return loop.call_args.args[0].transparent

    def test_no_option_restores_cache(self):
        self.assertTrue(self._run(None, True))

    def test_transparent_overrides_opaque_cache(self):
        self.assertTrue(self._run("--transparent", False))

    def test_no_transparent_overrides_transparent_cache(self):
        self.assertFalse(self._run("--no-transparent", True))

    def test_corrupt_progress_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": tmp}):
            progress = Progress()
            path = "/tmp/book.pdf"
            with open(progress._file(path), "w") as fh:
                fh.write("{not json!!")
            self.assertEqual(progress.load(path), {
                "page": 0, "invert": False, "crop": False,
                "transparent": False,
            })
            with open(progress._file(path), "w") as fh:
                json.dump({"page": "seventeen"}, fh)
            self.assertEqual(progress.load(path)["page"], 0)
            with open(progress._file(path), "w") as fh:
                json.dump({"page": None}, fh)
            self.assertEqual(progress.load(path)["page"], 0)

    def test_progress_cache_evicts_oldest_entries(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"XDG_CACHE_HOME": tmp}):
            progress = Progress()
            first = "/tmp/old-book.pdf"
            progress.save(first, 1)
            old_file = progress._file(first)
            os.utime(old_file, (0, 0))  # make it clearly the oldest
            for i in range(app._MAX_CACHE_ENTRIES):
                progress.save(f"/tmp/book-{i}.pdf", 1)
            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(
                os.path.exists(progress._file("/tmp/book-0.pdf")))

    @mock.patch("kittypdf.app.graphics.place")
    @mock.patch("kittypdf.app.graphics.send_image")
    @mock.patch("kittypdf.app.graphics.delete_image")
    @mock.patch("kittypdf.app.graphics.delete_all")
    def test_cli_choice_is_remembered_on_eof(
            self, _all, _delete, _send, _place):
        doc = FakeDoc()
        term = FakeTerm([("eof", "")])
        progress = mock.Mock()
        progress.load.return_value = {
            "page": 0, "invert": False, "crop": False,
            "transparent": True,
        }
        with mock.patch("kittypdf.app.os.path.isfile", return_value=True), \
                mock.patch("kittypdf.app.Document", return_value=doc), \
                mock.patch("kittypdf.app.Terminal", return_value=term), \
                mock.patch("kittypdf.app.Progress", return_value=progress), \
                mock.patch("kittypdf.app.graphics.probe", return_value=True):
            self.assertEqual(app.main(["--no-transparent", "book.pdf"]), 0)
        self.assertFalse(progress.save.call_args.args[-1])


class TermDisplayTests(unittest.TestCase):
    def _status_line(self, reader):
        last = reader.term.output[-1]
        visible = last.replace(f"\x1b[{reader.term.rows};1H", "")
        visible = visible.replace("\x1b[2K", "")
        return visible

    def test_status_truncates_long_message_to_screen_width(self):
        reader = Reader(FakeDoc(), FakeTerm())
        reader.status(msg="错" * 100)  # 200 display cells on an 80-col term
        line = self._status_line(reader)
        self.assertEqual(app._dw(line), reader.term.cols)
        self.assertLess(app._dw(line), 200)

    def test_status_keeps_short_message_intact(self):
        reader = Reader(FakeDoc(), FakeTerm())
        reader.status(msg="hello")
        self.assertIn("hello", self._status_line(reader))

    def test_setup_failure_prints_after_terminal_restore(self):
        order = []

        class OrderTerm(FakeTerm):
            def exit(self):
                order.append("exit")

        class OrderStderr:
            def write(self, text):
                if text.strip():
                    order.append(("print", text))
                return len(text)

            def flush(self):
                pass

        term = OrderTerm()
        with mock.patch("kittypdf.app.os.path.isfile", return_value=True), \
                mock.patch("kittypdf.app.Document",
                           return_value=FakeDoc()), \
                mock.patch("kittypdf.app.Terminal", return_value=term), \
                mock.patch("kittypdf.app.graphics.probe",
                           return_value=False), \
                mock.patch("kittypdf.app.graphics.delete_all"), \
                mock.patch("sys.stderr", new=OrderStderr()):
            self.assertEqual(app.main(["book.pdf"]), 2)
        self.assertEqual(order[0], "exit")
        printed = "".join(item[1] for item in order if item != "exit")
        self.assertIn("kitty graphics", printed)


class TocHintTests(unittest.TestCase):
    def test_toc_hint_never_exceeds_screen_or_cuts_cjk(self):
        term = FakeTerm()
        term.cols = 20  # narrower than the hint line
        toc = [(1, "章", 1)]
        import kittypdf.toc as toc_mod
        toc_mod._draw(term, toc, 0, 0)
        hint = term.output[-1].replace(f"\x1b[{term.rows};1H", "")
        self.assertLessEqual(app._dw(hint), term.cols)
        # no lone trailing combining half of a wide char: width parity matches
        for ch in hint:
            self.assertIn(app._dw(ch), (1, 2))


class GfxQuietTests(unittest.TestCase):
    class Term:
        def __init__(self):
            self.fd = 0
            self.written = []

        def write(self, text):
            self.written.append(text.encode())

        def write_bytes(self, data):
            self.written.append(data)

        def pending(self):
            return False

    def test_fire_and_forget_commands_use_q2(self):
        term = self.Term()
        graphics.send_image(term, 7, b"\x89PNGfake")
        graphics.place(term, 7, 1, 1)
        graphics.delete_image(term, 7)
        graphics.delete_all(term)
        blob = b"".join(term.written)
        self.assertNotIn(b"q=1", blob)
        self.assertGreaterEqual(blob.count(b"q=2"), 4)
        # the support probe still expects a reply (no quiet flag in a=q)
        term2 = self.Term()
        with mock.patch("kittypdf.graphics.select.select",
                        return_value=[[], [], []]):
            graphics.probe(term2)
        self.assertNotIn(b"q=2", b"".join(term2.written))

    def test_probe_drains_pushback_before_selecting(self):
        term = self.Term()
        term.fd = -1  # would explode if select() were called on it
        chunks = [b"jj\x1b_G;i=32;OK\x1b\\"]  # user keys + probe response
        calls = {"select": 0, "read_raw": 0}

        def fake_select(*_args):
            calls["select"] += 1
            return [[], [], []]

        def fake_read_raw(_size=4096):
            calls["read_raw"] += 1
            return chunks.pop(0) if chunks else b""

        term.pending = lambda: bool(chunks)
        term.read_raw = fake_read_raw
        with mock.patch("kittypdf.graphics.select.select",
                        side_effect=fake_select):
            self.assertTrue(graphics.probe(term))
        self.assertEqual(calls["select"], 0)   # pushback served first
        self.assertEqual(calls["read_raw"], 1)

    def test_apc_sequences_are_swallowed_not_decoded_as_keys(self):
        from kittypdf.term import Terminal
        term = Terminal.__new__(Terminal)  # no tty needed for decoding
        term._buf = (b"\x1b_Gq=2 payload bytes here\x1b\\"
                     b"\x1b_G;i=1;OK\x1b\\x")
        term._eof = False
        term.fd = -1
        self.assertEqual(term.read_key(timeout=0.05), ("char", "x"))
        self.assertEqual(term._buf, b"")

    def test_partial_apc_does_not_emit_phantom_key(self):
        from kittypdf.term import Terminal
        term = Terminal.__new__(Terminal)
        term._buf = b"\x1b_Gtruncated-without-st"
        term._eof = False
        term.fd = -1
        with mock.patch("kittypdf.term.select.select",
                        return_value=[[], [], []]):
            # incomplete APC must fall back to a lone-Escape event, never a
            # phantom 'G'/'_' char from the APC introducer
            event = term.read_key(timeout=0.05)
        self.assertIn(event, [("esc", ""), None])


class GfxDeleteAndProbeTests(unittest.TestCase):
    class Term:
        def __init__(self):
            self.written = []

        def write_bytes(self, data):
            self.written.append(data)

    def test_delete_image_uses_d_i(self):
        term = self.Term()
        graphics.delete_image(term, 7)
        self.assertEqual(term.written, [b"\x1b_Ga=d,d=i,i=7,q=2\x1b\\"])

    def test_probe_response_parsing_is_strict(self):
        parse = graphics._parse_probe_response
        # well-formed kitty reply, with keys typed around it
        self.assertTrue(parse(b"jj\x1b_G;i=1;OK\x1b\\k"))
        self.assertTrue(parse(b"\x1b_Gf=24,s=1,v=1,i=1;OK\x1b\\"))
        # substring match no longer suffices
        self.assertFalse(parse(b"garbage with OK inside"))
        self.assertFalse(parse(b"\x1b_G;i=1;NOTOK\x1b\\"))
        self.assertFalse(parse(b"\x1b_G;i=1;OK"))         # no ST terminator
        self.assertFalse(parse(b""))                       # nothing at all
        # error replies are failures even when well-formed
        self.assertFalse(parse(b"\x1b_G;i=1;ERR\x1b\\"))
        self.assertFalse(parse(b"\x1b_G;i=1;ERR:ENODATA\x1b\\"))
        # malformed control fields reject the frame
        self.assertFalse(parse(b"\x1b_Gjunk;OK\x1b\\"))


class OwnerPasswordTests(unittest.TestCase):
    def test_owner_password_pdf_opens_and_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "owner-only.pdf")
            src = pymupdf.open()
            page = src.new_page(width=100, height=100)
            page.insert_text((35, 55), "text")
            src.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     owner_pw="owner-secret", user_pw="",
                     permissions=pymupdf.PDF_PERM_PRINT)
            src.close()
            doc = Document(path)  # must not raise
            self.assertEqual(doc.page_count, 1)
            _png, w, _h = doc.render(0, 100, 100)
            self.assertGreater(w, 0)


class MainModuleTests(unittest.TestCase):
    def test_python_m_exits_1_on_missing_file(self):
        env = dict(os.environ)
        srcdir = os.path.join(os.path.dirname(__file__), "..", "src")
        env["PYTHONPATH"] = (srcdir + os.pathsep + env.get("PYTHONPATH", ""))
        proc = subprocess.run(
            [sys.executable, "-m", "kittypdf", "/nonexistent/book.pdf"],
            capture_output=True, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"no such file", proc.stderr)


if __name__ == "__main__":
    unittest.main()
