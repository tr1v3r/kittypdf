"""Page rendering: PyMuPDF page -> PNG bytes fitted to the viewport."""

import pymupdf

# Hard ceiling so an accidentally huge terminal cannot explode memory.
_MAX_PIXELS = 8_000_000


class Document:
    def __init__(self, path):
        self.path = path
        self.doc = pymupdf.open(path)
        self.page_count = self.doc.page_count

    def render(self, page_no, max_w, max_h, invert=False):
        """Render 0-based `page_no` to fit (max_w, max_h) pixels.

        Returns (png_bytes, width, height).
        """
        page = self.doc.load_page(page_no)
        rect = page.rect
        zoom = min(max_w / max(rect.width, 1), max_h / max(rect.height, 1))
        zoom = min(zoom, (_MAX_PIXELS / (rect.width * rect.height)) ** 0.5)
        zoom = max(zoom, 0.01)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        if invert:
            pix = _inverted(pix)
        return pix.tobytes("png"), pix.width, pix.height


def _inverted(pix):
    """Invert all channels via a translation table (C speed, no per-byte loop)."""
    samples = bytearray(pix.samples).translate(bytes(range(255, -1, -1)))
    return pymupdf.Pixmap(pix.colorspace, pix.width, pix.height, samples, False)
