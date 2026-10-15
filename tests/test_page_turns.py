"""Page-turn wire ordering, bounded placements and linear/batched transmission."""

import base64
import re
import unittest
from unittest import mock

from kittypdf import app, graphics
from kittypdf.app import Reader
from kittypdf.term import Terminal


class WireTerm:
    xpixel = 800
    rows = 31
    cols = 80
    cell_h = 20
    cell_w = 10

    def __init__(self):
        self.output = []

    def write(self, value):
        self.output.append(value.encode())

    def write_bytes(self, value):
        self.output.append(value)

    @property
    def wire(self):
        return b"".join(self.output)


class Doc:
    page_count = 5

    def page_size(self, page, autocrop=False):
        return 100, 140

    def render(self, page, width, height, **options):
        return f"png{page}".encode(), 100 + page, 140


def commands(wire):
    return [({key: value for key, value in
              (item.split(b"=", 1) for item in head.split(b","))}, payload)
            for head, payload in re.findall(rb"\x1b_G([^;\x1b]*)(?:;([^\x1b]*))?\x1b\\",
                                            wire)]


class StorageTerm(WireTerm):
    """Minimal Kitty image lifetime model, not just an output recorder.

    ED2 frees unplaced uploads too (graphics.c grman_clear/filter_refs),
    whereas EL2 must leave graphics alone. Reject placing a missing bitmap.
    """

    def __init__(self):
        super().__init__()
        self.images = set()
        self.placements = set()

    def write(self, value):
        super().write(value)
        if "\x1b[2J" in value:
            self.images.clear()
            self.placements.clear()

    def write_bytes(self, value):
        super().write_bytes(value)
        for cmd, _ in commands(value):
            action = cmd.get(b"a")
            image_id = int(cmd.get(b"i", b"0"))
            if action == b"t":
                self.images.add(image_id)
            elif action == b"p":
                if image_id not in self.images:
                    raise AssertionError(f"ENOENT: image {image_id} lost before placement")
                self.placements.add(image_id)
            elif action == b"d":
                self.placements.discard(image_id)
                if cmd.get(b"d") == b"I":
                    self.images.discard(image_id)


class PageTurnTests(unittest.TestCase):
    def setUp(self):
        self.term = WireTerm()
        self.reader = Reader(Doc(), self.term)
        self.reader.mode = "single"

    def initial_frame(self, mode="single"):
        self.reader.mode = mode
        self.reader.draw()
        self.term.output.clear()

    def test_upload_precedes_short_sync_swap_and_retirement(self):
        self.initial_frame()
        self.reader.page = 1
        self.reader.draw()
        wire = self.term.wire
        begin = wire.index(b"\x1b[?2026h")
        end = wire.index(b"\x1b[?2026l")
        before = commands(wire[:begin])
        commit = commands(wire[begin:end])
        self.assertEqual([cmd[b"a"] for cmd, _ in before], [b"t"])
        self.assertEqual(before[0][0][b"i"], b"3")
        self.assertEqual([cmd[b"a"] for cmd, _ in commit], [b"p", b"d"])
        self.assertEqual(commit[-1][0], {b"a": b"d", b"d": b"I",
                                         b"i": b"1", b"q": b"2"})
        self.assertIn(b"[2/5]s", wire[begin:end])  # footer is in the SAME commit
        self.assertNotIn(b"\x1b[2J", wire)
        self.assertEqual(wire[end:], b"\x1b[?2026l")

    def test_both_new_pages_uploaded_before_either_old_page_is_deleted(self):
        self.initial_frame("dual")
        self.reader.page = 2
        self.reader.draw()
        wire = self.term.wire
        begin = wire.index(b"\x1b[?2026h")
        self.assertEqual([cmd[b"i"] for cmd, _ in commands(wire[:begin])],
                         [b"3", b"4"])
        commit = [cmd for cmd, _ in commands(wire[begin:])]
        self.assertEqual([cmd[b"a"] for cmd in commit], [b"p", b"p", b"d", b"d"])
        self.assertEqual([cmd[b"i"] for cmd in commit], [b"3", b"4", b"1", b"2"])

    def test_banks_alternate_and_old_ids_are_freed(self):
        for page, ids in ((0, (1,)), (1, (3,)), (2, (1,)), (3, (3,))):
            self.reader.page = page
            self.reader.draw()
            self.assertEqual(self.reader._active, ids)
        self.assertEqual({cmd[b"i"] for cmd, _ in commands(self.term.wire)},
                         {b"1", b"3"})

    def test_unchanged_frame_does_not_upload_or_create_placements(self):
        self.reader.transparent = True
        self.initial_frame()
        for _ in range(20):
            self.reader.draw()
        self.assertEqual(commands(self.term.wire), [])
        self.assertNotIn(b"\x1b[?2026h", self.term.wire)

    def test_invalidate_does_not_blank_until_new_upload_is_complete(self):
        self.initial_frame()
        self.reader.invalidate()
        self.assertEqual(self.term.wire, b"")
        self.reader.draw()
        wire = self.term.wire
        self.assertLess(wire.index(b"a=t"), wire.index(b"\x1b[?2026h"))
        self.assertNotIn(b"\x1b[2J", wire)
        self.assertLess(wire.index(b"\x1b[?2026h"), wire.index(b"\x1b[2K"))
        self.assertLess(wire.index(b"\x1b[2K"), wire.index(b"a=p"))
        self.assertLess(wire.index(b"a=p"), wire.index(b"\x1b[?2026l"))

    def test_first_draw_after_loop_invalidation_preserves_staged_images(self):
        for mode, expected in (("single", {1}), ("dual", {1, 2})):
            with self.subTest(mode=mode):
                term = StorageTerm()
                term.take_resize = lambda: False
                term.read_key = lambda timeout=None: ("char", "q")
                reader = Reader(Doc(), term)
                reader.mode = mode
                app._loop(reader, reader.doc, term, "book.pdf", mock.Mock())
                self.assertEqual(term.images, expected)
                self.assertEqual(term.placements, expected)

    def test_refresh_resize_and_overlay_restore_preserve_staged_images(self):
        for clear_overlay in (False, True):
            with self.subTest(clear_overlay=clear_overlay):
                term = StorageTerm()
                reader = Reader(Doc(), term)
                reader.mode = "dual"
                reader.draw()
                if clear_overlay:
                    term.write("\x1b[2J")  # an overlay may erase existing images
                reader.invalidate()
                reader.draw()
                self.assertEqual(term.images, {3, 4})
                self.assertEqual(term.placements, {3, 4})
                term.xpixel = 1000
                reader.invalidate()
                reader.draw()
                self.assertEqual(term.images, {1, 2})
                self.assertEqual(term.placements, {1, 2})

    def test_dual_to_single_and_odd_last_page_retire_both_old_pages(self):
        for mode, page in (("single", 2), ("dual", 4)):
            with self.subTest(mode=mode):
                self.setUp()
                self.initial_frame("dual")
                self.reader.mode = mode
                self.reader.page = page
                self.reader.draw()
                cmds = [cmd for cmd, _ in commands(self.term.wire)]
                self.assertEqual([cmd[b"a"] for cmd in cmds], [b"t", b"p", b"d", b"d"])
                self.assertEqual(self.reader._active, (3,))
                self.assertEqual([cmd[b"i"] for cmd in cmds[-2:]], [b"1", b"2"])

    def test_single_and_spread_centering_is_preserved(self):
        self.reader.draw()
        self.assertIn(b"\x1b[12;36H", self.term.wire)
        self.term.output.clear()
        self.reader.mode = "dual"
        self.reader.draw()
        self.assertIn(b"\x1b[12;30H", self.term.wire)
        self.assertIn(b"\x1b[12;41H", self.term.wire)

    def test_render_error_keeps_front_frame_and_error_status(self):
        self.initial_frame("dual")
        old_sig = self.reader._sent
        old_bank = self.reader._bank
        self.reader.page = 2
        self.reader.invalidate()
        with mock.patch.object(self.reader.doc, "render", side_effect=[
                (b"left", 100, 140), RuntimeError("bad right page")]):
            self.reader.draw()
        self.assertEqual(commands(self.term.wire), [])
        self.assertNotIn(b"\x1b[2J", self.term.wire)
        self.assertIn(b"render error: bad right page", self.term.wire)
        self.assertEqual(self.reader._active, (1, 2))
        self.assertEqual(self.reader._bank, old_bank)
        self.assertIsNone(self.reader._sent)
        self.assertIsNotNone(old_sig)

    def test_upload_error_never_clears_or_starts_sync(self):
        self.initial_frame()
        self.reader.page = 1
        old_sig = self.reader._sent
        with mock.patch.object(graphics, "send_image", side_effect=OSError("closed")), \
                self.assertRaises(OSError):
            self.reader.draw()
        self.assertEqual(self.term.wire, b"")
        self.assertEqual(self.reader._active, (1,))
        self.assertEqual(self.reader._sent, old_sig)

    def test_loop_does_not_overwrite_render_error_with_default_footer(self):
        self.term.take_resize = lambda: False
        self.term.read_key = mock.Mock(side_effect=[("char", "e"), ("char", "q")])
        original = self.reader.doc.render
        self.reader.doc.render = lambda p, *a, **kw: (
            original(p, *a, **kw) if p == 0 else self.fail_render())
        app._loop(self.reader, self.reader.doc, self.term, "book.pdf", mock.Mock())
        self.assertIn(b"render error: bad page", self.term.output[-1])

    @staticmethod
    def fail_render():
        raise RuntimeError("bad page")


class TransferTests(unittest.TestCase):
    def test_chunk_sizes_continuation_keys_payload_and_batched_writes(self):
        for size in (1, 3072, 3073, 1024 * 1024):
            with self.subTest(size=size):
                term = WireTerm()
                png = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
                graphics.send_image(term, 7, png)
                frames = commands(term.wire)
                self.assertEqual(base64.b64decode(b"".join(p for _, p in frames)), png)
                first = frames[0][0]
                self.assertEqual((first[b"a"], first[b"i"], first[b"f"]),
                                 (b"t", b"7", b"100"))
                for index, (cmd, payload) in enumerate(frames):
                    self.assertLessEqual(len(payload), 4096)
                    self.assertEqual(len(payload) % 4, 0)
                    self.assertEqual(cmd[b"q"], b"2")
                    self.assertEqual(cmd[b"m"], b"0" if index == len(frames) - 1 else b"1")
                    if index:
                        self.assertEqual(set(cmd), {b"m", b"q"})
                self.assertLessEqual(max(map(len, term.output)), graphics._WRITE_BATCH + 4200)
                if size >= 1024 * 1024:
                    self.assertLess(len(term.output), len(frames) // 10)

    def test_explicit_placement_id_makes_repeated_place_idempotent(self):
        term = WireTerm()
        graphics.place(term, 7, 2, 3)
        graphics.place(term, 7, 3, 4)
        self.assertEqual([cmd[b"p"] for cmd, _ in commands(term.wire)], [b"1", b"1"])

    def test_sync_always_ends_even_if_presentation_raises(self):
        term = WireTerm()
        with self.assertRaises(RuntimeError), graphics.synchronized(term):
            raise RuntimeError("broken frame")
        self.assertEqual(term.wire, b"\x1b[?2026h\x1b[?2026l")

    def test_terminal_exit_defensively_ends_sync(self):
        term = Terminal.__new__(Terminal)
        term._attrs = None
        term.write = mock.Mock()
        term.exit()
        self.assertTrue(term.write.call_args.args[0].startswith("\x1b[?2026l"))


if __name__ == "__main__":
    unittest.main()
