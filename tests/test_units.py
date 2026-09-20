"""Unit tests for the paths the original suite missed.

Focus (all chosen because bugs have lived here or are known-open):
- term.py key decoding: CSI/SS3 sequences, kitty CSI-u, APC swallowing,
  UTF-8 across pushback splits, lone ESC, alt-folding (TERM-2 territory).
- render.py autocrop: blank pages, dark scans, crop caching.
- toc.py: helpers + toc_loop driven by a fake terminal (the overlay-stuck
  regression class from REVIEW_2026).
- app.py _loop key dispatch: counts, gg/G, toggles, ToC round-trip,
  resize, ctrl keys (colemak mapping regression net).
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import pymupdf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kittypdf import term as term_mod  # noqa: E402
from kittypdf import toc  # noqa: E402
from kittypdf.app import Reader, _loop  # noqa: E402
from kittypdf.render import Document  # noqa: E402


def make_terminal(buf=b""):
    """A Terminal wired only for decode testing (no fd/termios touched)."""
    t = term_mod.Terminal.__new__(term_mod.Terminal)
    t._buf = bytes(buf)
    t._eof = False
    t._winch = False
    t.rows, t.cols = 31, 80
    t.xpixel = t.ypixel = 0
    t.cell_w = t.cell_h = 0.0
    return t


class DecodePlainTests(unittest.TestCase):
    def decode(self, buf):
        return make_terminal(buf)._decode()

    def test_c0_and_specials(self):
        self.assertEqual(self.decode(b"\r"), ("enter", "\r"))
        self.assertEqual(self.decode(b"\n"), ("enter", "\r"))
        self.assertEqual(self.decode(b"\x7f"), ("backspace", ""))
        self.assertEqual(self.decode(b"\x08"), ("backspace", ""))
        self.assertEqual(self.decode(b"\x03"), ("ctrl", "C"))
        self.assertEqual(self.decode(b"\x1f"), ("ctrl", "_"))
        self.assertEqual(self.decode(b"x"), ("char", "x"))
        self.assertEqual(self.decode(b" "), ("char", " "))

    def test_utf8_multibyte_whole(self):
        t = make_terminal("中".encode())
        self.assertEqual(t._decode(), ("char", "中"))
        self.assertEqual(t._buf, b"")

    def test_utf8_split_across_pushback(self):
        # TERM-2 territory: a codepoint split between read()s must not be
        # lost; the decoder must ask for more bytes, not eat them singly.
        raw = "中".encode()
        t = make_terminal(raw[:1])
        self.assertIsNone(t._decode())          # incomplete: keep the byte
        self.assertEqual(t._buf, raw[:1])
        t._buf += raw[1:]
        self.assertEqual(t._decode(), ("char", "中"))
        self.assertEqual(t._buf, b"")

    def test_invalid_utf8_becomes_single_placeholder(self):
        t = make_terminal(b"\xff\xfe\xfd\xfc")
        self.assertEqual(t._decode(), ("char", "?"))
        self.assertEqual(t._buf, b"")


class DecodeEscapeTests(unittest.TestCase):
    def decode(self, buf):
        return make_terminal(buf)._decode()

    def test_csi_arrows_and_tilde(self):
        for seq, want in [
            (b"\x1b[A", ("key", "up")), (b"\x1b[B", ("key", "down")),
            (b"\x1b[C", ("key", "right")), (b"\x1b[D", ("key", "left")),
            (b"\x1b[H", ("key", "home")), (b"\x1b[F", ("key", "end")),
            (b"\x1b[5~", ("key", "pgup")), (b"\x1b[6~", ("key", "pgdn")),
            (b"\x1b[1~", ("key", "home")), (b"\x1b[4~", ("key", "end")),
            (b"\x1b[2~", ("key", "ignored")),
        ]:
            self.assertEqual(self.decode(seq), want, seq)

    def test_ss3_arrows(self):
        self.assertEqual(self.decode(b"\x1bOA"), ("key", "up"))
        self.assertEqual(self.decode(b"\x1bOB"), ("key", "down"))

    def test_unknown_csi_final_is_ignored_and_consumed(self):
        t = make_terminal(b"\x1b[9Zj")
        self.assertEqual(t._decode(), ("key", "ignored"))
        self.assertEqual(t._decode(), ("char", "j"))

    def test_incomplete_csi_waits(self):
        self.assertIsNone(self.decode(b"\x1b["))

    def test_csi_u_variants(self):
        self.assertEqual(self.decode(b"\x1b[97u"), ("char", "a"))
        self.assertEqual(self.decode(b"\x1b[13u"), ("enter", "\r"))
        self.assertEqual(self.decode(b"\x1b[27u"), ("esc", ""))
        self.assertEqual(self.decode(b"\x1b[127u"), ("backspace", ""))
        self.assertEqual(self.decode(b"\x1b[9u"), ("char", "\t"))
        self.assertEqual(self.decode(b"\x1b[97;5u"), ("ctrl", "A"))
        self.assertEqual(self.decode(b"\x1b[97;3:2u"), ("char", "a"))  # alt, no ctrl
        self.assertEqual(self.decode(b"\x1b[xu"), ("key", "ignored"))
        self.assertEqual(self.decode(b"\x1b[99999999999u"), ("key", "ignored"))

    def test_apc_swallowed_whole_then_next_key_decoded(self):
        t = make_terminal(b"\x1b_Gi=31;EINVAL:nope\x1b\\\x1b[A")
        self.assertEqual(t._decode(), ("key", "up"))  # gfx reply never leaks

    def test_incomplete_apc_waits_for_terminator(self):
        self.assertIsNone(self.decode(b"\x1b_Gi=31;OK"))

    def test_esc_plus_char_folds_to_base_key(self):
        self.assertEqual(self.decode(b"\x1bx"), ("char", "x"))


class ReadKeyTests(unittest.TestCase):
    def test_sequential_events_from_one_buffer(self):
        t = make_terminal(b"2j")
        self.assertEqual(t.read_key(timeout=0), ("char", "2"))
        self.assertEqual(t.read_key(timeout=0), ("char", "j"))
        # buffer empty, no data, deadline already reached -> None
        self.assertIsNone(t.read_key(timeout=0))

    def test_lone_esc_committed_when_nothing_follows(self):
        t = make_terminal(b"\x1b")
        t._eof = True  # no more input will ever arrive
        self.assertEqual(t.read_key(), ("esc", ""))

    def test_eof_is_sticky_after_drain(self):
        t = make_terminal(b"q")
        t._eof = True
        self.assertEqual(t.read_key(), ("char", "q"))
        self.assertEqual(t.read_key(), ("eof", ""))
        self.assertEqual(t.read_key(), ("eof", ""))

    def test_pending_reflects_pushback(self):
        t = make_terminal(b"ab")
        t._decode()
        self.assertTrue(t.pending())


class TocHelperTests(unittest.TestCase):
    def test_dw_counts_cjk_as_two_cells(self):
        self.assertEqual(toc._dw("abc"), 3)
        self.assertEqual(toc._dw("中文"), 4)
        self.assertEqual(toc._dw("a中b"), 4)

    def test_fit_never_splits_a_wide_glyph(self):
        # 5 cells budget: "中中" needs 4, adding "a" needs 1 more -> fits;
        # but "中中中" would need 6 -> cut before the third glyph.
        self.assertEqual(toc._fit("中中a", 5), "中中a")
        self.assertEqual(toc._fit("中中中", 5), "中中")

    def test_initial_sel_finds_current_chapter(self):
        chapters = [(1, "c1", 1), (1, "c2", 5), (2, "c3", 12)]
        self.assertEqual(toc._initial_sel(chapters, 0), 0)
        self.assertEqual(toc._initial_sel(chapters, 4), 1)
        self.assertEqual(toc._initial_sel(chapters, 99), 2)

    def test_clamp_sel(self):
        entries = [(1, "a", 1)] * 3
        self.assertEqual(toc._clamp_sel(entries, -5), 0)
        self.assertEqual(toc._clamp_sel(entries, 9), 2)


class FakeTocTerm:
    def __init__(self, events, cols=80, rows=31):
        self.events = list(events)
        self.output = []
        self.cols, self.rows = cols, rows

    def write(self, text):
        self.output.append(text)

    def read_key(self, timeout=None):
        return self.events.pop(0) if self.events else ("eof", "")


TOC = [(1, "Chapter One", 3), (1, "Chapter Two", 7), (2, "Deep dive", 11)]


class TocLoopTests(unittest.TestCase):
    def run_toc(self, events, page0=0, toc_list=TOC):
        t = FakeTocTerm(events)
        result = toc.toc_loop(t, toc_list, page0)
        return result, t

    def test_empty_toc_cancels(self):
        self.assertEqual(toc.toc_loop(FakeTocTerm([]), [], 0), ("cancel", None))

    def test_enter_jumps_to_selected_chapter(self):
        result, _ = self.run_toc([("enter", "\r")])
        self.assertEqual(result, ("jump", 2))  # page 3, 1-based -> 0-based 2

    def test_move_then_enter(self):
        result, _ = self.run_toc([("char", "j"), ("enter", "\r")])
        self.assertEqual(result, ("jump", 6))

    def test_count_multiplies_move(self):
        result, _ = self.run_toc([("char", "2"), ("char", "j"),
                                  ("enter", "\r")])
        self.assertEqual(result, ("jump", 10))

    def test_up_clamps_at_top(self):
        result, _ = self.run_toc([("char", "k"), ("enter", "\r")])
        self.assertEqual(result, ("jump", 2))

    def test_big_jumps(self):
        entries = [(1, f"chapter {i}", i + 1) for i in range(25)]
        result, _ = self.run_toc([("char", "E"), ("char", "E"), ("char", "U"),
                                  ("enter", "\r")], toc_list=entries)
        # E: +10, E: +10, U: -10 -> sel 10
        self.assertEqual(result, ("jump", entries[10][2] - 1))

    def test_gg_and_G(self):
        result, _ = self.run_toc([("char", "G"), ("char", "g"), ("char", "g"),
                                  ("enter", "\r")])
        self.assertEqual(result, ("jump", 2))

    def test_arrow_keys_and_home_end(self):
        result, _ = self.run_toc([("key", "down"), ("enter", "\r")])
        self.assertEqual(result, ("jump", 6))
        result, _ = self.run_toc([("key", "end"), ("key", "up"),
                                  ("enter", "\r")])
        self.assertEqual(result, ("jump", 6))

    def test_q_esc_cancel_ctrl_c_eof(self):
        self.assertEqual(self.run_toc([("char", "q")])[0], ("cancel", None))
        self.assertEqual(self.run_toc([("esc", "")])[0], ("cancel", None))
        self.assertEqual(self.run_toc([("ctrl", "C")])[0], ("eof", None))

    def test_t_does_not_close_overlay(self):
        # 't' must not toggle the overlay closed (key-repeat oscillation).
        result, t = self.run_toc([("char", "t"), ("enter", "\r")])
        self.assertEqual(result, ("jump", 2))
        self.assertGreater(len(t.output), 1)  # it kept drawing, stayed open

    def test_draw_survives_narrow_terminal(self):
        narrow = FakeTocTerm([("enter", "\r")], cols=10, rows=4)
        toc.toc_loop(narrow, TOC, 0)  # must not raise

    def test_unknown_key_clears_count(self):
        result, _ = self.run_toc([("char", "2"), ("char", "z"),
                                  ("char", "j"), ("enter", "\r")])
        self.assertEqual(result, ("jump", 6))  # count was reset by 'z'


class AutocropTests(unittest.TestCase):
    def make_pdf(self, draw=None):
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        doc = pymupdf.open()
        page = doc.new_page(width=612, height=792)  # US Letter
        if draw:
            draw(page)
        doc.save(path)
        doc.close()
        self.addCleanup(os.unlink, path)
        return path

    def test_blank_page_has_no_content_rect(self):
        d = Document(self.make_pdf())
        self.assertIsNone(d._content_rect(0, d.doc.load_page(0)))
        w, h = d.page_size(0, autocrop=True)
        self.assertAlmostEqual(w, 612, delta=1)
        self.assertAlmostEqual(h, 792, delta=1)

    def test_content_rect_bounds_text_with_padding(self):
        def draw(page):
            page.insert_text((200, 300), "tiny", fontsize=12)
        d = Document(self.make_pdf(draw))
        rect = d._content_rect(0, d.doc.load_page(0))
        self.assertIsNotNone(rect)
        self.assertLess(rect.width, 612)
        self.assertLess(rect.height, 792)
        self.assertGreaterEqual(rect.x0, 150)   # content around x=200
        self.assertLessEqual(rect.x1, 350)
        self.assertGreaterEqual(rect.y0, 200)   # baseline y=300
        self.assertLessEqual(rect.y1, 350)
        # padding honoured on every side
        self.assertLessEqual(rect.width, 100 + 2 * 8 + 5)
        self.assertLessEqual(rect.height, 12 + 2 * 8 + 5)

    def test_dark_scan_comes_back_as_full_page(self):
        def draw(page):
            page.draw_rect(pymupdf.Rect(0, 0, 612, 792), fill=(0, 0, 0))
        d = Document(self.make_pdf(draw))
        rect = d._content_rect(0, d.doc.load_page(0))
        self.assertIsNotNone(rect)
        self.assertGreater(rect.width, 0.95 * 612)
        self.assertGreater(rect.height, 0.95 * 792)

    def test_crop_cache_returns_same_object(self):
        def draw(page):
            page.insert_text((100, 100), "x", fontsize=10)
        d = Document(self.make_pdf(draw))
        first = d._content_rect(0, d.doc.load_page(0))
        second = d._content_rect(0, d.doc.load_page(0))
        self.assertIs(first, second)

    def test_cropped_render_larger_than_full_page_render(self):
        def draw(page):
            # a square of content in the middle of a tall page: cropping it
            # lets the square use the full 400x400 box instead of being
            # height-limited by the 792pt page.
            page.draw_rect(pymupdf.Rect(150, 250, 450, 550), fill=(0, 0, 0))
        d = Document(self.make_pdf(draw))
        _, w_full, h_full = d.render(0, 400, 400)
        _, w_crop, h_crop = d.render(0, 400, 400, autocrop=True)
        self.assertGreater(w_crop * h_crop, w_full * h_full)


class LoopFakeTerm:
    xpixel = 800
    ypixel = 620
    rows = 31
    cols = 80
    cell_w = 10.0
    cell_h = 20.0
    has_pixel_size = True

    def __init__(self, events, resizes=()):
        self.events = list(events)
        self.resizes = list(resizes)
        self.output = []
        self.drains = 0

    def write(self, value):
        self.output.append(value)

    def write_bytes(self, data):
        self.output.append(data)

    def take_resize(self):
        return self.resizes.pop(0) if self.resizes else False

    def read_key(self, timeout=None):
        return self.events.pop(0) if self.events else ("eof", "")

    def drain_typeahead(self):
        self.drains += 1


class LoopFakeDoc:
    page_count = 8
    toc = [(1, "Chapter One", 3), (1, "Chapter Two", 5)]

    def page_size(self, page, autocrop=False):
        return 100, 140

    def render(self, page, width, height, **options):
        return b"png", 100, 140


def run_loop(events, resizes=()):
    term = LoopFakeTerm(events, resizes)
    doc = LoopFakeDoc()
    reader = Reader(doc, term)
    progress = mock.MagicMock()
    with mock.patch("kittypdf.app.graphics.delete_all"), \
            mock.patch("kittypdf.app.graphics.delete_image"), \
            mock.patch("kittypdf.app.graphics.send_image"), \
            mock.patch("kittypdf.app.graphics.place"):
        _loop(reader, doc, term, "/loop.pdf", progress)
    return reader, term, progress


class LoopDispatchTests(unittest.TestCase):
    def test_quit_checkpoints_progress(self):
        _, _, progress = run_loop([("char", "q")])
        progress.save.assert_called_once()

    def test_colemak_step_keys_and_counts(self):
        reader, _, _ = run_loop([("char", "2"), ("char", "e"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 2)
        reader, _, _ = run_loop([("char", "3"), ("char", "u"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 0)  # clamped

    def test_gg_with_count_and_G(self):
        reader, _, _ = run_loop([("char", "3"), ("char", "g"), ("char", "g"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 2)
        reader, _, _ = run_loop([("char", "G"), ("char", "q")])
        self.assertEqual(reader.page, 7)

    def test_toggle_keys(self):
        reader, _, _ = run_loop([("char", "i"), ("char", "a"), ("char", "c"),
                                 ("char", "q")])
        self.assertTrue(reader.invert)
        self.assertTrue(reader.transparent)
        self.assertTrue(reader.autocrop)

    def test_d_cycles_mode_and_invalidates(self):
        reader, _, _ = run_loop([("char", "d"), ("char", "q")])
        self.assertEqual(reader.mode, "single")
        reader, _, _ = run_loop([("char", "d"), ("char", "d"), ("char", "q")])
        self.assertEqual(reader.mode, "dual")
        reader, _, _ = run_loop([("char", "d"), ("char", "d"), ("char", "d"),
                                 ("char", "q")])
        self.assertEqual(reader.mode, "auto")

    def test_arrow_home_end_enter(self):
        reader, _, _ = run_loop([("key", "down"), ("key", "pgdn"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 2)
        reader, _, _ = run_loop([("key", "end"), ("key", "home"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 0)
        reader, _, _ = run_loop([("enter", "\r"), ("char", "q")])
        self.assertEqual(reader.page, 1)

    def test_toc_round_trip_jump_and_cancel(self):
        reader, term, _ = run_loop([("char", "t"), ("enter", "\r"),
                                    ("char", "q")])
        self.assertEqual(reader.page, 2)          # jumped to chapter page 3
        self.assertEqual(term.drains, 2)          # drained on entry and exit
        reader, _, _ = run_loop([("char", "t"), ("char", "q"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 0)          # cancel keeps the page

    def test_toc_eof_quits_app(self):
        reader, _, progress = run_loop([("char", "t"), ("ctrl", "C")])
        self.assertEqual(reader.page, 0)
        progress.save.assert_called_once()

    def test_ctrl_l_invalidates_without_moving(self):
        reader, _, _ = run_loop([("char", "2"), ("char", "e"), ("ctrl", "L"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 2)

    def test_resize_triggers_redraw_not_crash(self):
        reader, _, _ = run_loop([("char", "q")], resizes=[True])
        self.assertEqual(reader.page, 0)

    def test_unrelated_key_clears_pending_count(self):
        reader, _, _ = run_loop([("char", "2"), ("char", "z"), ("char", "e"),
                                 ("char", "q")])
        self.assertEqual(reader.page, 1)  # step 1, not 2

    def test_ignored_sequence_never_touches_count(self):
        reader, _, _ = run_loop([("key", "ignored"), ("char", "2"),
                                 ("char", "e"), ("char", "q")])
        self.assertEqual(reader.page, 2)


if __name__ == "__main__":
    unittest.main()
