"""EPUB support: MuPDF reflows EPUBs into pages; the Document wrapper and
TOC path must work unchanged (regression net for the epub enablement)."""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kittypdf.render import Document  # noqa: E402


def make_epub(path, chapters, css=None):
    """Write a minimal EPUB3; chapters is [(title, href, body), ...].

    With css, it is linked from every chapter — for fixtures that trip
    MuPDF's CSS parser (e.g. device-font @font-face src urls).
    """
    head = ""
    if css is not None:
        head = ('<head><link rel="stylesheet" type="text/css" '
                'href="style.css"/></head>')
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
        if css is not None:
            z.writestr("OEBPS/style.css", css)
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
        for _, href, body in chapters:
            z.writestr(f"OEBPS/{href}", f"<html>{head}<body>{body}</body></html>")


class TestEpub(unittest.TestCase):
    def test_open_render_toc(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "book.epub")
        make_epub(path, [
            ("Chapter One", "c1.xhtml", "<h1>One</h1><p>text</p>"),
            ("Chapter Two", "c2.xhtml", "<h1>Two</h1><p>text</p>"),
        ])
        doc = Document(path)
        self.addCleanup(doc.doc.close)
        self.assertEqual(doc.page_count, 2)
        self.assertEqual(doc.toc, [
            [1, "Chapter One", 1],
            [1, "Chapter Two", 2],
        ])
        for page_no in range(doc.page_count):
            w, h = doc.page_size(page_no)
            self.assertGreater(w, 0)
            self.assertGreater(h, 0)
            png, pw, ph = doc.render(page_no, 400, 600)
            self.assertGreater(len(png), 0)
            self.assertLessEqual(pw, 400)
            self.assertLessEqual(ph, 600)

    def test_render_is_silent_on_device_font_css(self):
        # Sony-style CSS references reader-builtin fonts by device path;
        # MuPDF cannot find them, falls back, and used to print one
        # "MuPDF error" per reference to stderr — garbling the TUI.
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "sony.epub")
        make_epub(path, [("One", "c1.xhtml", "<p>text</p>")],
                  css="@font-face { font-family: sony; "
                      "src: url(res:///opt/sony/ebook/FONT/tt0011m_.ttf); } "
                      "body { font-family: sony; }")
        srcdir = os.path.join(os.path.dirname(__file__), "..", "src")
        code = (f"import sys; sys.path.insert(0, {srcdir!r}); "
                f"from kittypdf.render import Document; "
                f"doc = Document({path!r}); "
                f"assert doc.render(0, 400, 600)[0]; doc.doc.close()")
        # C-level stderr is only reliably observable in a fresh process.
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")


if __name__ == "__main__":
    unittest.main()
