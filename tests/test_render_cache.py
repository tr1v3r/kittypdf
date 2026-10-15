"""Bounded, per-document recent-page PNG caching."""

import gc
import os
import tempfile
import unittest
import weakref
from itertools import product
from unittest import mock

import pymupdf

from kittypdf import render
from kittypdf.render import Document


class RenderCacheTests(unittest.TestCase):
    def make_document(self):
        source = mock.Mock(needs_pass=False, page_count=4)
        source.get_toc.return_value = []
        with mock.patch.object(render.pymupdf, "open", return_value=source):
            return Document("book.pdf")

    def assert_cache_bytes(self, doc, expected):
        self.assertEqual(doc._render_cache_bytes, expected)
        self.assertEqual(sum(len(result[0]) for result in doc._render_cache.values()),
                         expected)

    def test_repeat_bypasses_real_render_and_png_encoding(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "book.pdf")
        with pymupdf.open() as source:
            page = source.new_page(width=100, height=150)
            page.insert_text((20, 50), "cached text")
            source.save(path)
        doc = Document(path)
        self.addCleanup(doc.doc.close)

        with mock.patch.object(doc, "_render_uncached", wraps=doc._render_uncached) as uncached, \
                mock.patch.object(doc.doc, "load_page", wraps=doc.doc.load_page) as load:
            first = doc.render(0, 200, 300)
            second = doc.render(0, 200, 300, invert=False, autocrop=False,
                                transparent=False)
            third = doc.render(0, 200, 300, False, False, False)

        self.assertIs(first, second)
        self.assertIs(first, third)
        self.assertTrue(first[0].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(first[1:], (200, 300))
        self.assertEqual(uncached.call_count, 1)
        self.assertEqual(load.call_count, 1)
        self.assert_cache_bytes(doc, len(first[0]))

    def test_page_and_each_viewport_dimension_have_separate_entries(self):
        doc = self.make_document()
        requests = [(0, 200, 300), (1, 200, 300), (0, 201, 300), (0, 200, 301)]
        results = [(bytes([i]), 10 + i, 20 + i) for i in range(len(requests))]
        with mock.patch.object(doc, "_render_uncached", side_effect=results) as uncached:
            for request, result in zip(requests, results, strict=True):
                self.assertEqual(doc.render(*request), result)
            for request, result in zip(requests, results, strict=True):
                self.assertEqual(doc.render(*request), result)
        self.assertEqual(uncached.call_count, len(requests))
        self.assert_cache_bytes(doc, len(requests))

    def test_all_render_option_combinations_are_isolated(self):
        doc = self.make_document()
        options = list(product((False, True), repeat=3))
        results = [(bytes([i]), 10 + i, 20 + i) for i in range(len(options))]
        with mock.patch.object(doc, "_render_uncached", side_effect=results) as uncached:
            for flags, result in zip(options, results, strict=True):
                self.assertEqual(doc.render(0, 200, 300, *flags), result)
            for flags, result in zip(options, results, strict=True):
                self.assertEqual(doc.render(0, 200, 300, invert=flags[0],
                                            autocrop=flags[1], transparent=flags[2]), result)
        self.assertEqual(uncached.call_args_list, [
            mock.call(0, 200, 300, *flags) for flags in options])
        self.assert_cache_bytes(doc, len(options))

    def test_entry_limit_evicts_least_recently_used_not_oldest_inserted(self):
        doc = self.make_document()
        with mock.patch.object(render, "_RENDER_CACHE_MAX_ENTRIES", 2), \
                mock.patch.object(doc, "_render_uncached",
                                  return_value=(b"png", 10, 20)) as uncached:
            for page in (0, 1, 0, 2, 0, 2):
                doc.render(page, 200, 300)
            self.assertEqual(uncached.call_count, 3)
            self.assertEqual([key[0] for key in doc._render_cache], [0, 2])
            self.assert_cache_bytes(doc, 6)
            doc.render(1, 200, 300)
            self.assertEqual(uncached.call_count, 4)
            self.assertEqual([key[0] for key in doc._render_cache], [2, 1])
            self.assert_cache_bytes(doc, 6)

    def test_byte_limit_accepts_exact_budget_and_evicts_multiple_lru_entries(self):
        doc = self.make_document()
        results = [(b"a" * 4, 10, 20), (b"b" * 6, 10, 20),
                   (b"c" * 7, 10, 20), (b"d" * 4, 10, 20)]
        with mock.patch.object(render, "_RENDER_CACHE_MAX_BYTES", 10), \
                mock.patch.object(doc, "_render_uncached", side_effect=results) as uncached:
            doc.render(0, 200, 300)
            doc.render(1, 200, 300)
            self.assertEqual(len(doc._render_cache), 2)
            self.assert_cache_bytes(doc, 10)
            doc.render(0, 200, 300)  # hit promotes page 0 without counting bytes twice
            self.assert_cache_bytes(doc, 10)
            doc.render(2, 200, 300)  # neither old result fits beside the new PNG
            self.assertEqual([key[0] for key in doc._render_cache], [2])
            self.assert_cache_bytes(doc, 7)
            doc.render(0, 200, 300)  # evicted page renders again
            self.assertEqual(uncached.call_count, 4)
            self.assertEqual([key[0] for key in doc._render_cache], [0])
            self.assert_cache_bytes(doc, 4)

    def test_oversized_result_is_not_cached_or_allowed_to_displace_recent_pages(self):
        doc = self.make_document()

        def result(page, *_args):
            return b"x" * (11 if page == 2 else 4), 10, 20

        with mock.patch.object(render, "_RENDER_CACHE_MAX_ENTRIES", 2), \
                mock.patch.object(render, "_RENDER_CACHE_MAX_BYTES", 10), \
                mock.patch.object(doc, "_render_uncached", side_effect=result) as uncached:
            doc.render(0, 200, 300)
            doc.render(1, 200, 300)
            self.assertEqual(len(doc.render(2, 200, 300)[0]), 11)
            doc.render(2, 200, 300)
            self.assertEqual(uncached.call_count, 4)
            self.assertEqual([key[0] for key in doc._render_cache], [0, 1])
            self.assert_cache_bytes(doc, 8)
            doc.render(3, 200, 300)
            self.assertEqual([key[0] for key in doc._render_cache], [1, 3])
            self.assert_cache_bytes(doc, 8)

    def test_zero_entry_or_byte_budget_disables_caching(self):
        for limit in ("_RENDER_CACHE_MAX_ENTRIES", "_RENDER_CACHE_MAX_BYTES"):
            with self.subTest(limit=limit):
                doc = self.make_document()
                with mock.patch.object(render, limit, 0), \
                        mock.patch.object(doc, "_render_uncached",
                                          return_value=(b"png", 10, 20)) as uncached:
                    doc.render(0, 200, 300)
                    doc.render(0, 200, 300)
                self.assertEqual(uncached.call_count, 2)
                self.assertFalse(doc._render_cache)
                self.assert_cache_bytes(doc, 0)

    def test_failure_is_not_cached_or_allowed_to_evict_successful_results(self):
        doc = self.make_document()
        result = (b"png", 10, 20)
        with mock.patch.object(render, "_RENDER_CACHE_MAX_ENTRIES", 1), \
                mock.patch.object(doc, "_render_uncached", side_effect=[
                    result, RuntimeError("render failed"), result]) as uncached:
            doc.render(0, 200, 300)
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                doc.render(1, 200, 300)
            self.assertEqual(doc.render(0, 200, 300), result)
            self.assertEqual([key[0] for key in doc._render_cache], [0])
            self.assert_cache_bytes(doc, 3)
            self.assertEqual(doc.render(1, 200, 300), result)
            self.assertEqual(doc.render(1, 200, 300), result)
            self.assertEqual(uncached.call_count, 3)
            self.assert_cache_bytes(doc, 3)

    def test_documents_do_not_share_cache_or_byte_accounting(self):
        first = self.make_document()
        second = self.make_document()
        with mock.patch.object(first, "_render_uncached", return_value=(b"first", 10, 20)), \
                mock.patch.object(second, "_render_uncached",
                                  return_value=(b"second", 30, 40)) as uncached:
            self.assertEqual(first.render(0, 200, 300), (b"first", 10, 20))
            self.assertEqual(second.render(0, 200, 300), (b"second", 30, 40))
        self.assertEqual(uncached.call_count, 1)
        self.assertIsNot(first._render_cache, second._render_cache)
        self.assert_cache_bytes(first, 5)
        self.assert_cache_bytes(second, 6)

    def test_render_cache_does_not_keep_document_alive(self):
        doc = self.make_document()
        with mock.patch.object(doc, "_render_uncached", return_value=(b"png", 10, 20)):
            doc.render(0, 200, 300)
        reference = weakref.ref(doc)
        del doc
        gc.collect()
        self.assertIsNone(reference())


if __name__ == "__main__":
    unittest.main()
