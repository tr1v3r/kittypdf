"""EPUB support: MuPDF reflows EPUBs into pages; the Document wrapper and
TOC path must work unchanged (regression net for the epub enablement)."""

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kittypdf.render import Document  # noqa: E402


def make_epub(chapters):
    """Minimal EPUB3 with a nav TOC; chapters is [(title, href, body), ...]."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "book.epub")
    manifest = "\n".join(
        f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, (_, href, _) in enumerate(chapters))
    manifest += ('<item id="nav" href="nav.xhtml" '
                 'media-type="application/xhtml+xml" properties="nav"/>')
    spine = "\n".join(
        f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    nav_items = "\n".join(
        f'<li><a href="{href}">{title}</a></li>'
        for title, href, _ in chapters)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", f'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">kittypdf-test</dc:identifier>
    <dc:title>Test</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>{manifest}</manifest>
  <spine>{spine}</spine>
</package>''')
        z.writestr("OEBPS/nav.xhtml", f'''<html
  xmlns="http://www.w3.org/1999/xhtml"
  xmlns:epub="http://www.idpf.org/2007/ops">
  <body><nav epub:type="toc"><ol>{nav_items}</ol></nav></body>
</html>''')
        for i, (_, href, body) in enumerate(chapters):
            z.writestr(f"OEBPS/{href}", f"<html><body>{body}</body></html>")
    return path


class TestEpub(unittest.TestCase):
    def test_open_render_toc(self):
        path = make_epub([
            ("Chapter One", "c1.xhtml", "<h1>One</h1><p>text</p>"),
            ("Chapter Two", "c2.xhtml", "<h1>Two</h1><p>text</p>"),
        ])
        doc = Document(path)
        self.assertEqual(doc.page_count, 2)
        self.assertEqual([entry[1] for entry in doc.toc],
                         ["Chapter One", "Chapter Two"])
        for page_no in range(doc.page_count):
            w, h = doc.page_size(page_no)
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)
            png, pw, ph = doc.render(page_no, 400, 600)
            self.assertGreater(len(png), 0)
            self.assertLessEqual(pw, 400)
            self.assertLessEqual(ph, 600)


if __name__ == "__main__":
    unittest.main()
