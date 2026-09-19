"""Page rendering: PyMuPDF page -> PNG bytes fitted to the viewport."""

import pymupdf

# Hard ceiling so an accidentally huge terminal cannot explode memory.
_MAX_PIXELS = 8_000_000

# Autocrop probe: render small, find the bounding box of non-paper pixels.
_CROP_PROBE_W = 200     # probe pixmap target width in pixels
_CROP_THRESHOLD = 245   # a channel below this counts as content
_CROP_PADDING = 8       # points of breathing room kept around the content


class Document:
    def __init__(self, path):
        self.path = path
        self.doc = pymupdf.open(path)
        self.page_count = self.doc.page_count
        self.toc = self.doc.get_toc()  # [[level, title, page_1based], ...]
        self._crop_cache = {}

    def page_size(self, page_no, autocrop=False):
        """(width, height) of the page in points, honouring autocrop."""
        page = self.doc.load_page(page_no)
        rect = page.rect
        if autocrop:
            content = self._content_rect(page_no, page)
            if content is not None:
                rect = content
        return max(rect.width, 1.0), max(rect.height, 1.0)

    def render(self, page_no, max_w, max_h, invert=False, autocrop=False,
               transparent=False):
        """Render 0-based `page_no` to fit (max_w, max_h) pixels.

        Returns (png_bytes, width, height). With transparent=True, unpainted
        areas retain their alpha; explicit backgrounds and scans are not erased.
        """
        page = self.doc.load_page(page_no)
        rect = page.rect
        if autocrop:
            content = self._content_rect(page_no, page)
            if content is not None:
                rect = content
        zoom = min(max_w / max(rect.width, 1), max_h / max(rect.height, 1))
        zoom = min(zoom, (_MAX_PIXELS / (rect.width * rect.height)) ** 0.5)
        zoom = max(zoom, 0.01)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                              alpha=transparent, clip=rect)
        if invert:
            pix = _inverted(pix)
        return pix.tobytes("png"), pix.width, pix.height

    def _content_rect(self, page_no, page):
        """Bounding box of page content in page coordinates, or None.

        Probes the page at low resolution and finds the outermost
        non-paper pixels (works for text and scans alike; pages whose
        paper is darker than the threshold simply come back as the full
        page, i.e. no crop). Results are cached per page.
        """
        if page_no in self._crop_cache:
            return self._crop_cache[page_no]
        rect = page.rect
        zoom = max(_CROP_PROBE_W / max(rect.width, 1), 0.05)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        samples = pix.samples
        stride = pix.width * 3
        threshold = _CROP_THRESHOLD

        y0 = y1 = None
        for y in range(pix.height):
            row = samples[y * stride:(y + 1) * stride]
            if row and min(row) < threshold:
                if y0 is None:
                    y0 = y
                y1 = y
        if y0 is None:
            self._crop_cache[page_no] = None
            return None

        x0 = x1 = None
        for x in range(pix.width):
            base = x * 3
            column = samples[y0 * stride + base:y1 * stride + base + 1:stride]
            if min(column) < threshold:
                if x0 is None:
                    x0 = x
                x1 = x
        if x0 is None:  # pragma: no cover - y-scan already found content
            self._crop_cache[page_no] = None
            return None

        pad = _CROP_PADDING
        cropped = pymupdf.Rect(
            max(rect.x0, rect.x0 + x0 / zoom - pad),
            max(rect.y0, rect.y0 + y0 / zoom - pad),
            min(rect.x1, rect.x0 + (x1 + 1) / zoom + pad),
            min(rect.y1, rect.y0 + (y1 + 1) / zoom + pad),
        )
        self._crop_cache[page_no] = cropped
        return cropped


def _inverted(pix):
    """Invert color while retaining premultiplied alpha.

    MuPDF stores RGBA samples premultiplied (C <= A). The inverted premultiplied
    color is therefore A - C, not 255 - C; changing alpha would make transparent
    paper opaque and produce halos around anti-aliased text.
    """
    if not pix.alpha:
        samples = bytearray(pix.samples).translate(bytes(range(255, -1, -1)))
        return pymupdf.Pixmap(pix.colorspace, pix.width, pix.height,
                              samples, False)

    inverted = pymupdf.Pixmap(pix)
    inverted.invert_irect()
    return inverted
