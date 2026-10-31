"""
PDF Viewer for Sublime Text 4.

Opening a .pdf file renders its pages and shows them as a scrollable stack of
images in a read-only view, the way a desktop viewer like evince would.

MuPDF rasterizes the pages on the CPU: a long-running `mutool run` process
(mupdf_helper.js) keeps the document open, caches page display lists and writes
each strip of a page to a PNG file. Sublime draws those in block phantoms.
poppler's pdftoppm is the fallback without mutool.

Sublime doesn't tell plugins about clicks on phantoms, so on X11 a thread watches
the mouse buttons itself (XInput2) to select text and follow links.
"""

import base64
import ctypes
import hashlib
import html
import json
import math
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import zlib

import sublime
import sublime_plugin

SETTINGS_FILE = "PDFViewer.sublime-settings"
IS_VIEWER = "pdf_viewer"          # view setting marking a viewer view (used by keymap context)
VIEWER_PATH = "pdf_viewer_path"   # view setting: path of the PDF shown
RAW_MARK = "pdf_viewer_raw"       # view setting: user asked to see this PDF as hex
STATE_KEY = "pdf_viewer_state"    # view setting: page, zoom and colors, kept across reloads
STATUS_KEY = "pdf_viewer"
PHANTOM_KEY = "pdf_viewer"
SEARCH_CONTEXT = "pdf_viewer_search"   # keymap context: a search is active in this viewer
SELECTION_CONTEXT = "pdf_viewer_selection"   # keymap context: text is selected in this viewer
HELPER_JS = "mupdf_helper.js"

LETTER = (612.0, 792.0)
PHANTOM_DX, PHANTOM_DY = 1, 2   # Sublime draws block phantoms this far right of / below their spot
BAND_PT = 256.0   # pages are rendered in equal strips at most this tall

# Search and selection highlights drawn into the page images: (rgb, alpha).
MATCH_MARK = ([1.0, 0.8, 0.0], 0.35)
CURRENT_MARK = ([1.0, 0.4, 0.0], 0.55)
SELECT_MARK = ([0.2, 0.5, 1.0], 0.35)

VIEW_SETTINGS = {
    "gutter": False,
    "line_numbers": False,
    "highlight_line": False,
    "draw_indent_guides": False,
    "draw_white_space": "none",
    "word_wrap": True,
    "rulers": [],
    "scroll_past_end": False,
    "spell_check": False,
    "auto_complete": False,
    "fold_buttons": False,
    "margin": 0,
    "caret_extra_width": 0,
    "drag_text": False,
    "mini_diff": False,
    "show_definitions": False,
    "scroll_context_lines": 0,
    "match_brackets": False,
    "highlight_gutter": False,
    "translate_tabs_to_spaces": False,
    "detect_indentation": False,
}

_docs = {}      # view.id() -> PdfDoc
_pending = set()  # ids of hex views currently being converted
_helper_js = None  # mupdf_helper.js on disk, found in plugin_loaded


# --------------------------------------------------------------------------
# helpers

def settings():
    return sublime.load_settings(SETTINGS_FILE)


def log(*args):
    s = settings()
    if not s.get("debug"):
        return
    line = "[PDFViewer] " + " ".join(str(a) for a in args)
    print(line)
    path = s.get("debug_log_file")
    if path:
        try:
            with open(os.path.expanduser(path), "a") as fh:
                fh.write(time.strftime("%H:%M:%S ") + line + "\n")
        except Exception:
            pass


def tool(name):
    paths = settings().get("tool_paths") or {}
    explicit = paths.get(name)
    if explicit:
        return os.path.expanduser(explicit)
    found = shutil.which(name)
    if found:
        return found
    for extra in ("/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
        candidate = os.path.join(extra, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run(cmd, timeout=300):
    kwargs = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, **kwargs)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def cache_root():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "sublime-pdf-viewer")


def find_helper_js():
    """mupdf_helper.js on disk; copied into the cache when the package is zipped."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), HELPER_JS)
    if os.path.isfile(here):
        return here
    try:
        data = sublime.load_resource("Packages/%s/%s" % (__package__, HELPER_JS))
    except Exception as e:
        print("[PDFViewer] %s is missing: %s" % (HELPER_JS, e))
        return None
    digest = hashlib.sha1(data.encode("utf-8")).hexdigest()[:8]
    path = os.path.join(cache_root(), "mupdf_helper-%s.js" % digest)
    if not os.path.exists(path):
        os.makedirs(cache_root(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data)
    return path


def band_edges(height):
    """Page-space y edges of the strips a page is rendered in. Strips are short so that a
    change to the highlights only redraws the strips it touches. They don't depend on the
    zoom, so a strip rendered at the old zoom can stand in while the new one renders."""
    count = max(1, int(math.ceil(height / BAND_PT - 0.01)))
    return [height * k / count for k in range(count + 1)]


def data_uri(png):
    """The PNG inlined as a data: URI. Sublime shows a broken-image icon for a frame
    whenever it rebuilds a phantom that loads files; inlined images draw at once."""
    if isinstance(png, str):
        with open(png, "rb") as fh:
            png = fh.read()
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def placeholder_uri():
    """A 1x1 translucent grey PNG, scaled by <img width/height> to stand in for unrendered pages."""

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    raw = b"\x00" + bytes([128, 128, 128, 40])  # filter byte + RGBA
    return data_uri(b"\x89PNG\r\n\x1a\n" +
                    chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) +
                    chunk(b"IDAT", zlib.compress(raw)) +
                    chunk(b"IEND", b""))


PLACEHOLDER = placeholder_uri()


def viewer_color_scheme(view):
    """Write (once) a color scheme derived from the view's current one that hides the caret
    and selection, and return its Packages path. Returns None if that fails."""
    try:
        style = view.style()
        bg = style.get("background", "#272822")
        fg = style.get("foreground", "#f8f8f2")
        digest = hashlib.sha1((bg + fg).encode("utf-8")).hexdigest()[:8]
        rel = "Packages/User/PDFViewer/PDFViewer-%s.sublime-color-scheme" % digest
        full = os.path.join(sublime.packages_path(), "User", "PDFViewer",
                            "PDFViewer-%s.sublime-color-scheme" % digest)
        if not os.path.exists(full):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            scheme = (
                '{\n'
                '\t// Generated by PDFViewer: the current color scheme with an invisible caret.\n'
                '\t"name": "PDF Viewer",\n'
                '\t"globals": {\n'
                '\t\t"background": "%s",\n'
                '\t\t"foreground": "%s",\n'
                '\t\t"caret": "#00000000",\n'
                '\t\t"block_caret": "#00000000",\n'
                '\t\t"selection": "%s",\n'
                '\t\t"selection_border": "%s",\n'
                '\t\t"inactive_selection": "%s",\n'
                '\t\t"line_highlight": "%s"\n'
                '\t}\n'
                '}\n' % (bg, fg, bg, bg, bg, bg))
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(scheme)
        return rel
    except Exception as e:
        log("color scheme:", e)
        return None


def apply_color_scheme(view, scheme, attempts=25):
    """Set the view's color scheme once Sublime's resource index knows the file
    (a freshly written file is picked up asynchronously)."""
    if not scheme or not view.is_valid():
        return
    name = scheme.rsplit("/", 1)[-1]
    if scheme in sublime.find_resources(name):
        view.settings().set("color_scheme", scheme)
    elif attempts > 0:
        sublime.set_timeout(lambda: apply_color_scheme(view, scheme, attempts - 1), 200)
    else:
        log("color scheme never became available:", scheme)


def is_pdf_path(path):
    return bool(path) and path.lower().endswith(".pdf")


def pdf_page_sizes(path):
    """Return a list of (width_pt, height_pt), one per page, rotation applied."""
    pdfinfo = tool("pdfinfo")
    if pdfinfo:
        rc, out, err = run([pdfinfo, "-f", "1", "-l", "1000000", path])
        if rc != 0:
            raise RuntimeError((err or out).strip() or "pdfinfo failed")
        sizes, rots = {}, {}
        for m in re.finditer(r"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", out, re.M):
            sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
        for m in re.finditer(r"^Page\s+(\d+)\s+rot:\s+(\d+)", out, re.M):
            rots[int(m.group(1))] = int(m.group(2))
        count = len(sizes)
        if not count:
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            count = int(m.group(1)) if m else 0
        if count <= 0:
            raise RuntimeError("PDF has no pages")
        pages = []
        for i in range(1, count + 1):
            w, h = sizes.get(i, LETTER)
            if rots.get(i, 0) % 180 == 90:
                w, h = h, w
            pages.append((max(w, 1.0), max(h, 1.0)))
        return pages

    mutool = tool("mutool")
    if mutool:
        rc, out, err = run([mutool, "info", "-M", path])
        if rc != 0:
            raise RuntimeError((err or out).strip() or "mutool failed")
        pages = []
        # e.g. "   1	(3 0 R):	[ 0 0 612 792 ]"
        for m in re.finditer(r"^\s*(\d+)\s+\(.*?\):\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", out, re.M):
            x0, y0, x1, y1 = (float(m.group(k)) for k in range(2, 6))
            pages.append((max(abs(x1 - x0), 1.0), max(abs(y1 - y0), 1.0)))
        if not pages:
            m = re.search(r"^Pages:\s+(\d+)", out, re.M)
            if m:
                pages = [LETTER] * int(m.group(1))
        if not pages:
            raise RuntimeError("could not read page list from mutool")
        return pages

    raise RuntimeError("No PDF tools found. Install poppler-utils (pdfinfo, pdftoppm) "
                       "or mupdf-tools (mutool).")


def pdf_text_pages(path):
    """Extract text, one string per page."""
    pdftotext = tool("pdftotext")
    if pdftotext:
        rc, out, err = run([pdftotext, "-layout", path, "-"])
        if rc != 0:
            raise RuntimeError((err or out).strip() or "pdftotext failed")
        pages = out.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        return pages
    mutool = tool("mutool")
    if mutool:
        rc, out, err = run([mutool, "draw", "-q", "-F", "txt", "-o", "-", path])
        if rc != 0:
            raise RuntimeError((err or out).strip() or "mutool failed")
        pages = out.split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        return pages
    raise RuntimeError("Text extraction needs pdftotext (poppler-utils) or mutool.")


# --------------------------------------------------------------------------
# MuPDF helper process

class Helper(object):
    """A long-running `mutool run mupdf_helper.js file.pdf`. It keeps the document
    open and answers one JSON request at a time."""

    def __init__(self, path):
        self.path = path
        self.proc = None
        self.closed = False
        self.lock = threading.Lock()       # one request at a time
        self.proc_lock = threading.Lock()  # guards proc against close()

    @staticmethod
    def command(path):
        mutool, stdbuf = tool("mutool"), tool("stdbuf")
        if not (mutool and stdbuf and _helper_js):
            return None
        # mutool doesn't flush its output when writing to a pipe; stdbuf line-buffers it.
        return [stdbuf, "-oL", mutool, "run", _helper_js, path]

    def _process(self):
        with self.proc_lock:
            if self.closed:
                raise RuntimeError("viewer closed")
            if self.proc is None or self.proc.poll() is not None:
                cmd = self.command(self.path)
                if not cmd:
                    raise RuntimeError("mutool or stdbuf not found")
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
            return self.proc

    def call(self, request, timeout=120):
        data = json.dumps(request)  # ASCII only, so it can be cut anywhere
        # The helper reads lines of at most 255 bytes: send "+chunk" lines and a final ".".
        chunks = ["+" + data[i:i + 200] for i in range(0, len(data), 200)] + ["."]
        with self.lock:
            proc = self._process()
            timer = threading.Timer(timeout, proc.kill)
            timer.start()
            try:
                proc.stdin.write(("\n".join(chunks) + "\n").encode("ascii"))
                proc.stdin.flush()
                line = proc.stdout.readline()
            except (OSError, ValueError):
                line = b""
            finally:
                timer.cancel()
            if not line:
                with self.proc_lock:
                    if self.proc is proc:
                        self.proc = None
                raise RuntimeError("viewer closed" if self.closed else "mutool stopped or timed out")
        reply = json.loads(line.decode("utf-8", "replace"))
        if "error" in reply:
            raise RuntimeError(reply["error"].split("\n")[0])
        return reply

    def close(self):
        with self.proc_lock:
            self.closed = True
            proc, self.proc = self.proc, None
        if proc:
            try:
                proc.kill()
            except OSError:
                pass
            threading.Thread(target=proc.wait, daemon=True).start()


# --------------------------------------------------------------------------
# search state

class Hit(object):
    __slots__ = ("quads", "text")

    def __init__(self, quads, text):
        self.quads = quads
        self.text = text


class Search(object):
    """Matches of one query, filled in page by page by a background thread."""

    def __init__(self, query, start, start_y, jump):
        self.query = query
        self.start = start        # page the search started on
        self.start_y = start_y    # layout y of the viewport top when it started
        self.jump = jump          # move to the first match once one is found
        self.pages = {}           # page -> [Hit], for pages searched so far
        self.searched = 0
        self.empty_pages = 0      # searched pages without any text
        self.current = None       # (page, index) of the selected match
        self.done = False
        self.error = None

    def ordered(self):
        return [(p, j) for p in sorted(self.pages) for j in range(len(self.pages[p]))]

    def total(self):
        return sum(len(hits) for hits in self.pages.values())


class Selection(object):
    """Text selected with the mouse, from anchor to head: each (page, [x, y]) in MuPDF's
    page space."""

    def __init__(self, anchor, head):
        self.anchor = anchor
        self.head = head
        self.quads = {}   # page -> (range, quads) the helper computed for that page

    def span(self):
        """(first page, its start, last page, its end) in reading order."""
        (pa, a), (pb, b) = self.anchor, self.head
        return (pa, a, pb, b) if pa <= pb else (pb, b, pa, a)

    def range_for(self, i):
        """(start, end) of the selection on page i, None if it doesn't reach the page.
        "start" and "end" stand for the page's first and last character."""
        p0, a, p1, b = self.span()
        if i < p0 or i > p1:
            return None
        if p0 == p1:
            return (a, b)
        return (a if i == p0 else "start", b if i == p1 else "end")


# --------------------------------------------------------------------------
# the document behind a viewer view

class PdfDoc(object):

    def __init__(self, view, path):
        self.view = view
        self.path = path
        self.pages = []           # [(w_pt, h_pt)]
        self.origins = []         # [(x0, y0)]: top-left of each page box in MuPDF's page space
        self.bands = []           # per page: y edges (points) of its strips, see band_edges()
        self.error = None
        s = settings()
        self.zoom_mode = s.get("default_zoom", "fit_width")
        self.zoom = 1.0
        self.dpi = 96.0
        self.invert = False
        self.margin = int(s.get("margin", 16))
        self.gap = int(s.get("page_gap", 10))
        self.prerender = max(0, int(s.get("prerender_pages", 3)))
        self.render_scale = max(0.5, float(s.get("render_scale", 1.0)))
        self.max_dpi = float(s.get("max_render_dpi", 400))
        self.renderer = s.get("renderer", "auto")
        self.threads = max(1, int(s.get("render_threads", 3)))

        self.engine = None        # "mupdf" (helper processes) or "poppler" (pdftoppm)
        self.query = None         # Helper for page sizes, search and the outline
        self.helpers = []         # one render Helper per worker thread

        self.lock = threading.Condition()
        self.want = []            # [(key, marks)]: strips to render, most urgent first
        self.rendered = {}        # key -> png path; key = (dpi, invert, page, strip, marks digest)
        self.failed = {}          # key -> message
        self.latest = {}          # (page, strip, invert) -> (seq, key, png) of its newest render
        self.taken = 0            # renders started so far: a later one shows a newer state
        self.in_progress = set()
        self.alive = True
        self.workers = []

        self.viewport_w = 0.0
        self.viewport_h = 0.0
        self.saved_state = None
        self.saved_at = 0.0
        self.resize_due = None
        self.window_pages = range(0)
        self.last_sig = None
        self.shown = set()        # png paths the phantoms show right now
        self.uris = {}            # png path -> data: URI, for the images on screen
        self.html_cache = {}      # page -> (state, html)
        self.text_pages = None
        self.outline = None
        self.search = None
        self.marks_cache = {}     # (page, strip) -> (digest, marks) for the search and selection
        self.phantom_ids = {}     # page -> (phantom id, html) of the phantom showing it
        self.phantom_layout = None  # (dpi, invert, width) the phantoms were sized for
        self.phantom_window = set()  # pages the phantoms show renders of (not placeholders)

        self.selection = None
        self.sel_lock = threading.Condition()
        self.sel_requests = {}    # page -> selection range waiting for its quads
        self.sel_busy = None      # (page, range) the helper is working on
        self.sel_thread = None
        self.links = {}           # page -> its links, read on the first click on the page
        self.history = []         # positions left by following links (alt+left)
        self.future = []          # positions left by going back (alt+right)

        try:
            st = os.stat(path)
            ident = "%s|%d|%d" % (path, int(st.st_mtime), st.st_size)
        except OSError:
            ident = path
        ident += "|strips%d" % BAND_PT
        digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:16]
        # One directory per view, so closing one tab can't delete another tab's renders.
        self.cache_dir = os.path.join(cache_root(), "%s-%d" % (digest, view.id()))

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        self.view.erase_phantoms(PHANTOM_KEY)
        self.phantom_ids = {}
        self.view.set_status(STATUS_KEY, "PDF: loading…")
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        pages, origins, engine, err = [], [], None, None
        if self.renderer != "pdftoppm" and Helper.command(self.path):
            query = Helper(self.path)
            try:
                for x0, y0, x1, y1 in query.call({"op": "info"})["pages"]:
                    pages.append((max(x1 - x0, 1.0), max(y1 - y0, 1.0)))
                    origins.append((x0, y0))
                engine, self.query = "mupdf", query
            except Exception as e:
                query.close()
                pages, origins, err = [], [], str(e)
                log("mutool helper failed:", e)
        if engine is None and self.renderer != "mutool":
            try:
                pages = pdf_page_sizes(self.path)
                origins = [(0.0, 0.0)] * len(pages)
                engine, err = "poppler", None
            except Exception as e:
                err = err or str(e)
        if engine is None and not err:
            err = "mutool could not be started (it needs mupdf-tools and stdbuf)"
        if engine and not pages:
            engine, err = None, "PDF has no pages"
        sublime.set_timeout(lambda: self._loaded(pages, origins, engine, err), 0)

    def _loaded(self, pages, origins, engine, err):
        if not self.alive or not self.view.is_valid():
            if self.query:
                self.query.close()
            return
        if err:
            self.error = err
            self._set_text("Could not open PDF\n\n%s\n\n%s\n\n"
                           "Press  e  to open it in an external viewer, or  h  to see the raw bytes."
                           % (self.path, err))
            self.view.set_status(STATUS_KEY, "PDF: error")
            log("load failed:", err)
            return
        self.pages, self.origins, self.engine = pages, origins, engine
        self.bands = [band_edges(h) for w, h in pages]
        # Where this view was before a plugin reload or a restart of Sublime.
        state = self.view.settings().get(STATE_KEY) or {}
        self.saved_at = time.time()   # don't overwrite it before it has been restored
        self.zoom_mode = state.get("zoom", self.zoom_mode)
        self.invert = bool(state.get("invert", self.invert))
        self._set_text("\n" * (len(pages) - 1))
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(0))
        self.view.set_viewport_position((0, 0), False)
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError as e:
            self.error = str(e)
        for i in range(self.threads):
            helper = Helper(self.path) if engine == "mupdf" else None
            if helper:
                self.helpers.append(helper)
            t = threading.Thread(target=self._worker, args=(helper,), name="pdf-render-%d" % i,
                                 daemon=True)
            t.start()
            self.workers.append(t)
        self._relayout(keep_position=False)
        if "page" in state:
            pos = (min(int(state["page"]), len(pages) - 1), float(state.get("frac", 0.0)))
            sublime.set_timeout(lambda: self._restore(pos), 40)
            sublime.set_timeout(lambda: self._restore(pos), 200)
        self._tick()
        log("opened", self.path, "pages:", len(pages), "engine:", engine)

    def _set_text(self, text):
        v = self.view
        v.set_read_only(False)
        v.run_command("select_all")
        v.run_command("right_delete")
        v.run_command("append", {"characters": text})
        v.set_read_only(True)

    def close(self, delete_cache=True):
        self.alive = False
        self.search = None
        self.selection = None
        with self.lock:
            self.lock.notify_all()
        with self.sel_lock:
            self.sel_lock.notify_all()
        for helper in self.helpers + ([self.query] if self.query else []):
            helper.close()
        if delete_cache:
            shutil.rmtree(self.cache_dir, ignore_errors=True)

    # -- geometry ------------------------------------------------------------

    def _page_px(self, i, dpi=None):
        w, h = self.pages[i]
        dpi = self.dpi if dpi is None else dpi
        return max(1, int(round(w * dpi / 72.0))), max(1, int(round(h * dpi / 72.0)))

    def _band_rows(self, i, dpi=None):
        """Pixel rows where the strips of page i start and end."""
        h_px = self._page_px(i, dpi)[1]
        h = self.pages[i][1]
        return [int(round(e * h_px / h)) for e in self.bands[i]]

    def _page_left(self, i):
        w = self._page_px(i)[0]
        vw = self.viewport_w
        left = int((vw - w) / 2) if vw else self.margin
        return max(self.margin, left)

    def _image_left(self, i):
        """Layout x of the first pixel column of page i."""
        return self._page_left(i) + PHANTOM_DX

    def _image_top(self, i):
        """Layout y of the first pixel row of page i: below its empty text line and the gap."""
        return self._row_y(i) + self.view.line_height() + self.gap + PHANTOM_DY

    def _layout_point(self, i, x, y):
        """Layout position of the point (x, y) in page i's MuPDF page space."""
        w, h = self.pages[i]
        w_px, h_px = self._page_px(i)
        ox, oy = self.origins[i]
        return (self._image_left(i) + (x - ox) * w_px / w,
                self._image_top(i) + (y - oy) * h_px / h)

    def point_at(self, lx, ly, clamp=False):
        """(page, x, y) in MuPDF page space under layout point (lx, ly), or None beside the
        pages. With clamp, the nearest point of the nearest page instead."""
        if not self.pages:
            return None
        i = self._row_at_y(ly)
        top = self._image_top(i)
        if ly < top and i > 0:
            # Between two pages: take the nearer one.
            above = self._image_top(i - 1) + self._page_px(i - 1)[1]
            if ly - above < top - ly:
                i, top = i - 1, self._image_top(i - 1)
        w_px, h_px = self._page_px(i)
        left = self._image_left(i)
        if not (left <= lx < left + w_px and top <= ly < top + h_px):
            if not clamp:
                return None
            lx = min(max(lx, left), left + w_px)
            ly = min(max(ly, top), top + h_px)
        w, h = self.pages[i]
        ox, oy = self.origins[i]
        return i, ox + (lx - left) * w / w_px, oy + (ly - top) * h / h_px

    def _compute_zoom(self):
        vw = self.viewport_w if self.viewport_w >= 50 else 800.0
        vh = self.viewport_h if self.viewport_h >= 50 else 600.0
        max_w = max(w for w, h in self.pages)
        max_h = max(h for w, h in self.pages)
        css_w = max_w / 72.0 * 96.0
        css_h = max_h / 72.0 * 96.0
        if self.zoom_mode == "fit_width":
            avail = max(100.0, vw - 2 * self.margin)
            zoom = avail / css_w
        elif self.zoom_mode == "fit_page":
            avail_w = max(100.0, vw - 2 * self.margin)
            avail_h = max(100.0, vh - self.gap - self.view.line_height() - 4)
            zoom = min(avail_w / css_w, avail_h / css_h)
        else:
            try:
                zoom = float(self.zoom_mode)
            except (TypeError, ValueError):
                zoom = 1.0
        zoom = max(0.05, min(8.0, zoom))
        dpi = max(8.0, min(self.max_dpi, 96.0 * zoom))
        self.zoom = zoom
        self.dpi = round(dpi, 1)

    def _row_y(self, row):
        # The buffer is one empty line per page, so line `row` starts at offset `row`.
        return self.view.text_to_layout(row)[1]

    def _row_at_y(self, y):
        lo, hi = 0, len(self.pages) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._row_y(mid) <= y:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _page_span(self, i):
        top = self._row_y(i)
        if i + 1 < len(self.pages):
            bottom = self._row_y(i + 1)
        else:
            bottom = self.view.layout_extent()[1]
        return top, max(bottom, top + 1)

    def _position(self):
        vy = self.view.viewport_position()[1]
        page = self._row_at_y(vy)
        top, bottom = self._page_span(page)
        return page, (vy - top) / float(bottom - top)

    def _restore(self, pos):
        if not self.alive or not self.view.is_valid() or not self.pages:
            return
        page, frac = pos
        page = min(page, len(self.pages) - 1)
        top, bottom = self._page_span(page)
        self.view.set_viewport_position((0, top + frac * (bottom - top)), False)

    def current_page(self):
        """0-based page whose top is at (or just above) the viewport top."""
        vy = self.view.viewport_position()[1]
        return self._row_at_y(vy + self.view.line_height() + self.gap + 2)

    def goto_page(self, page, animate=False):
        if not self.pages:
            return
        page = max(0, min(len(self.pages) - 1, page))
        self._scroll(page, lambda: (0, self._row_y(page)), animate)

    def scroll_to(self, page, y=None):
        """Show page-space height y of page (its top if y is None) at the top of the view."""
        if y is None:
            self.goto_page(page)
            return
        page = max(0, min(len(self.pages) - 1, page))
        self._scroll(page, lambda: (self.view.viewport_position()[0],
                                    max(0.0, self._layout_point(page, 0, y)[1] - self.gap)))

    def scroll_by(self, dy):
        vx, vy = self.view.viewport_position()
        bottom = max(0.0, self.view.layout_extent()[1] - self.view.viewport_extent()[1])
        self.view.set_viewport_position((vx, max(0.0, min(bottom, vy + dy))), False)
        self._update_visible()

    def _scroll(self, page, target, animate=False):
        pt = self.view.text_point(page, 0)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(pt))
        self.view.set_viewport_position(target(), animate)
        self.last_sig = None
        self._update_visible()

        # Panels closing or phantom relayout can swallow the scroll; re-apply once settled.
        def again():
            if self.alive and self.view.is_valid() and self.pages:
                self.view.set_viewport_position(target(), False)
        sublime.set_timeout(again, 120)

    # -- layout / phantoms ---------------------------------------------------

    def _relayout(self, keep_position=True):
        if not self.pages:
            return
        pos = self._position() if keep_position else None
        vw, vh = self.view.viewport_extent()
        if vw >= 50:
            self.viewport_w, self.viewport_h = vw, vh
        with self.lock:
            self._compute_zoom()
            self.lock.notify_all()
        widest = max(self._page_px(i)[0] for i in range(len(self.pages))) + 2 * self.margin
        self.view.settings().set("word_wrap", widest <= (self.viewport_w or 1e9))
        self.last_sig = None
        self._update_visible()
        self._prune()
        if pos is not None:
            sublime.set_timeout(lambda: self._restore(pos), 40)
            sublime.set_timeout(lambda: self._restore(pos), 200)

    def _tick(self):
        if not self.alive:
            return
        v = self.view
        if not v.is_valid():
            self.close()
            return
        if self.pages:
            vw, vh = v.viewport_extent()
            if vw >= 50:
                if self.viewport_w and (abs(vw - self.viewport_w) > 1 or abs(vh - self.viewport_h) > 1):
                    self.resize_due = time.time() + 0.25
                self.viewport_w, self.viewport_h = vw, vh
            if self.resize_due is not None and time.time() >= self.resize_due:
                self.resize_due = None
                if self.zoom_mode in ("fit_width", "fit_page"):
                    self._relayout(keep_position=True)
                else:
                    self.last_sig = None
                    self._update_visible()
            else:
                self._update_visible()
            self._save_state()
        sublime.set_timeout(self._tick, 150)

    def _save_state(self):
        """Keep the position, zoom and colors in the view's settings (at most once a second),
        so a plugin reload or a restart of Sublime comes back to them."""
        if time.time() - self.saved_at < 1.0:
            return
        page, frac = self._position()
        state = {"page": page, "frac": round(frac, 4), "zoom": self.zoom_mode, "invert": self.invert}
        if state != self.saved_state:
            self.view.settings().set(STATE_KEY, state)
            self.saved_state = state
        self.saved_at = time.time()

    def _update_visible_safe(self):
        if self.alive and self.view.is_valid() and self.pages:
            self._update_visible()

    def _update_visible(self):
        n = len(self.pages)
        v = self.view
        vy = v.viewport_position()[1]
        vh = self.viewport_h or v.viewport_extent()[1]
        first = self._row_at_y(vy)
        last = self._row_at_y(vy + max(vh, 1) - 1)
        lo = max(0, first - self.prerender)
        hi = min(n - 1, last + self.prerender)
        self.window_pages = range(lo, hi + 1)

        # Queue the strips of the pages around the viewport, nearest first, and pick
        # what each of them shows until its render is ready.
        jobs, display = [], {}
        with self.lock:
            for i in self.window_pages:
                top = self._image_top(i)
                rows = self._band_rows(i)
                states = []
                for k in range(len(rows) - 1):
                    key, marks = self._band_key(i, k)
                    dist = max(0.0, top + rows[k] - (vy + vh), vy - (top + rows[k + 1]))
                    jobs.append((dist, i, k, key, marks))
                    states.append(self._band_image(key))
                display[i] = tuple(states)
            jobs.sort(key=lambda job: job[:3])
            self.want = [(key, marks) for _, _, _, key, marks in jobs]
            self.lock.notify_all()

        v.set_status(STATUS_KEY, self._status(vy, vh))

        sig = (self.dpi, self.invert, int(self.viewport_w), tuple(sorted(display.items())))
        if sig == self.last_sig:
            return
        self.last_sig = sig
        self.shown = {png for states in display.values() for png, err in states if png}
        with self.lock:
            self.uris = {png: uri for png, uri in self.uris.items() if png in self.shown}
        # Only touch the phantoms whose page changed: going through all of them takes
        # 100 ms in a document with thousands of pages.
        layout = sig[:3]
        if layout != self.phantom_layout:
            pages = range(n)   # every placeholder changes size
            self.phantom_layout = layout
        else:
            pages = set(display) | self.phantom_window
        self.phantom_window = set(display)
        for i in pages:
            self._show_page(i, self._html(i, display.get(i)))

    def _show_page(self, i, content):
        """Show content in page i's phantom, if that isn't what it shows already."""
        old = self.phantom_ids.get(i)
        if old and old[1] == content:
            return
        # Line i of the buffer starts at offset i. Add the new phantom before erasing the
        # old one so the page never collapses in between.
        pid = self.view.add_phantom(PHANTOM_KEY, sublime.Region(i), content, sublime.LAYOUT_BLOCK)
        if old:
            self.view.erase_phantom_by_id(old[0])
        self.phantom_ids[i] = (pid, content)

    def _band_image(self, key):
        """(png, error) to show for a strip: its render, else a stand-in while it renders:
        the newest render at this zoom (older highlights), the plain one, or the newest at
        another zoom, stretched. Called with the lock held."""
        png = self.rendered.get(key)
        if png:
            return png, None
        if key in self.failed:
            return None, self.failed[key]
        dpi, invert, i, k, digest = key
        newest = self.latest.get((i, k, invert))
        if newest and newest[1][0] == dpi:
            return newest[2], None
        plain = self.rendered.get((dpi, invert, i, k, ""))
        return plain or (newest[2] if newest else None), None

    def _status(self, vy, vh):
        cur = self._row_at_y(vy + vh / 2.0)
        if self.zoom_mode == "fit_width":
            zoom_label = "fit width · %d%%" % round(self.zoom * 100)
        elif self.zoom_mode == "fit_page":
            zoom_label = "fit page · %d%%" % round(self.zoom * 100)
        else:
            zoom_label = "%d%%" % round(self.zoom * 100)
        text = "PDF  %d / %d   %s" % (cur + 1, len(self.pages), zoom_label)
        s = self.search
        if s:
            total = s.total()
            if s.error:
                found = "search failed"
            elif total:
                order = s.ordered()
                at = "%d of " % (order.index(s.current) + 1) if s.current in order else ""
                found = "%s%d match%s%s" % (at, total, "" if total == 1 else "es", "" if s.done else "…")
            elif not s.done:
                found = "searching…"
            elif s.empty_pages == s.searched:
                found = "no text in this PDF to search"
            else:
                found = "no matches"
            text += "   find \"%s\": %s" % (s.query, found)
        return text + "   (? for keys)"

    def _html(self, i, states):
        state = (self.dpi, self.invert, int(self.viewport_w), states)
        cached = self.html_cache.get(i)
        if cached and cached[0] == state:
            return cached[1]
        w, h = self._page_px(i)
        if states is None:
            # Far from the viewport: an empty page of the right size.
            body = self._img(PLACEHOLDER, w, h)
        else:
            rows = self._band_rows(i)
            parts = []
            for k, (png, err) in enumerate(states):
                if err:
                    parts.append('<div class="err">Page %d could not be rendered<br>%s</div>'
                                 % (i + 1, html.escape(err)))
                else:
                    parts.append(self._img(self._uri(png) if png else PLACEHOLDER,
                                           w, rows[k + 1] - rows[k]))
            body = "".join(parts)
        page = (
            '<body id="pdf-viewer">'
            '<style>'
            'body { margin: 0; padding: 0; }'
            '.page { margin: %dpx 0 0 %dpx; }'
            '.err { padding: 1rem; color: #e0605a; '
            'background-color: color(var(--foreground) alpha(0.06)); }'
            '</style>'
            '<div class="page">%s</div>'
            '</body>' % (self.gap, self._page_left(i), body)
        )
        self.html_cache[i] = (state, page)
        return page

    def _uri(self, png):
        with self.lock:
            uri = self.uris.get(png)
        if uri is None:
            try:
                uri = data_uri(png)
            except OSError:
                return PLACEHOLDER
            with self.lock:
                self.uris[png] = uri
        return uri

    @staticmethod
    def _img(uri, w, h):
        # One block per strip: stacked this way, strips touch without gaps.
        return '<div><img src="%s" width="%d" height="%d"></div>' % (uri, w, h)

    def _prune(self):
        """Delete renders for another zoom, colors or search, unless they are on screen or
        the newest render of their strip (which stands in while the strip re-renders)."""
        stale = []
        with self.lock:
            keep = self.shown | {png for seq, key, png in self.latest.values()}
            for key, png in list(self.rendered.items()):
                dpi, invert, i, k, digest = key
                if png in keep or (dpi == self.dpi and invert == self.invert and
                                   digest in ("", self._marks(i, k)[0])):
                    continue
                del self.rendered[key]
                stale.append(png)
        for png in stale:
            try:
                os.remove(png)
            except OSError:
                pass

    # -- zoom / mode ---------------------------------------------------------

    def set_zoom(self, mode):
        self.zoom_mode = mode
        self._relayout(keep_position=True)

    def zoom_by(self, delta):
        new = self.zoom * (1.0 + delta)
        self.set_zoom(round(max(0.1, min(8.0, new)), 3))

    def toggle_invert(self):
        if self.engine != "mupdf" and not (tool("magick") or tool("convert")):
            sublime.status_message("PDF Viewer: inverting needs mutool or ImageMagick")
            return
        self.invert = not self.invert
        self._relayout(keep_position=True)

    # -- rendering (worker threads) ------------------------------------------

    def _worker(self, helper):
        while True:
            with self.lock:
                job = None
                while job is None:
                    if not self.alive:
                        return
                    for key, marks in self.want:
                        if (key[:2] == (self.dpi, self.invert) and key not in self.rendered
                                and key not in self.failed and key not in self.in_progress):
                            job = (key, marks)
                            break
                    if job is None:
                        self.lock.wait()
                self.in_progress.add(job[0])
                self.taken += 1
                seq = self.taken
            key, marks = job
            started = time.time()
            ok, result = self._render(helper, key, marks)
            log("page %d strip %d at %s dpi: %.0f ms"
                % (key[2] + 1, key[3], key[0], (time.time() - started) * 1000))
            uri = None
            if ok:
                try:
                    uri = data_uri(result)
                except OSError as e:
                    ok, result = False, str(e)
            with self.lock:
                self.in_progress.discard(key)
                if not self.alive:
                    return
                if ok:
                    self.rendered[key] = result
                    slot = (key[2], key[3], key[1])
                    if seq > self.latest.get(slot, (0,))[0]:
                        self.latest[slot] = (seq, key, result)
                    self.uris[result] = uri
                else:
                    self.failed[key] = result
            if not ok:
                log("render failed page", key[2] + 1, "strip", key[3], ":", result)
            sublime.set_timeout(self._update_visible_safe, 0)

    def _render(self, helper, key, marks):
        dpi, invert, i, k, digest = key
        out = os.path.join(self.cache_dir, "p%d-%d-%s%s%s.png" % (
            i + 1, k, str(dpi).replace(".", "_"), "-inv" if invert else "",
            "-" + digest if digest else ""))
        if os.path.exists(out):
            return True, out
        tmp = out[:-4] + "-tmp.png"
        w, h = self.pages[i]
        w_px, h_px = self._page_px(i, dpi)
        rows = self._band_rows(i, dpi)
        scale = self.render_scale
        width = int(round(w_px * scale))
        top, bottom = int(round(rows[k] * scale)), int(round(rows[k + 1] * scale))
        try:
            if helper:
                x0, y0 = self.origins[i]
                helper.call({"op": "render", "page": i, "x0": x0, "y0": y0,
                             "sx": w_px * scale / w, "sy": h_px * scale / h,
                             "width": width, "top": top, "bottom": bottom,
                             "invert": invert, "marks": marks, "out": tmp})
            else:
                self._render_poppler(i, width, int(round(h_px * scale)), top, bottom, invert, tmp)
            os.replace(tmp, out)
            return True, out
        except Exception as ex:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False, str(ex)[:300]

    def _render_poppler(self, i, w_px, h_px, top, bottom, invert, out):
        pdftoppm = tool("pdftoppm")
        if not pdftoppm:
            raise RuntimeError("No PDF renderer found. Install mupdf-tools (mutool) "
                               "or poppler-utils (pdftoppm).")
        page = str(i + 1)
        rc, o, e = run([pdftoppm, "-png", "-f", page, "-l", page,
                        "-scale-to-x", str(w_px), "-scale-to-y", str(h_px),
                        "-x", "0", "-y", str(top), "-W", str(w_px), "-H", str(bottom - top),
                        "-singlefile", self.path, out[:-4]])
        # Renderers report recoverable syntax errors but still draw the page: trust the file.
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            msg = (e or o).strip().splitlines()
            raise RuntimeError(msg[-1] if msg else "pdftoppm exited with code %d" % rc)
        if invert:
            magick = tool("magick") or tool("convert")
            if not magick:
                raise RuntimeError("inverting colors needs mutool or ImageMagick")
            run([magick, out, "-negate", out])

    # -- search --------------------------------------------------------------

    def find(self, query, start=None, start_y=None):
        """Search the whole document from page start (default: the current page), mark all
        matches and jump to the first one at or after the viewport top."""
        query = query.strip()
        if not query:
            self.clear_search()
            return
        if start is None:
            start, start_y = self.current_page(), self.view.viewport_position()[1]
        s = Search(query, start, start_y, jump=True)
        self.search = s
        self._marks_changed()
        threading.Thread(target=self._search_pages, args=(s,), daemon=True).start()

    def _search_pages(self, s):
        n = len(self.pages)
        for step in range(n):
            if not self.alive or self.search is not s:
                return
            i = (s.start + step) % n
            try:
                reply = self.query.call({"op": "search", "page": i, "needle": s.query})
            except Exception as e:
                sublime.set_timeout(lambda e=e: self._search_failed(s, str(e)), 0)
                return
            hits = [Hit(h["quads"], h["text"]) for h in reply["hits"]]
            sublime.set_timeout(lambda i=i, hits=hits, empty=reply.get("empty"):
                                self._search_page_done(s, i, hits, empty), 0)
        sublime.set_timeout(lambda: self._search_done(s), 0)

    def _search_page_done(self, s, i, hits, empty):
        if self.search is not s or not self.alive:
            return
        s.searched += 1
        s.empty_pages += 1 if empty else 0
        if not hits:
            return
        s.pages[i] = hits
        if s.jump and s.current is None:
            for j, hit in enumerate(hits):
                if i != s.start or self._hit_box(i, hit)[1] >= s.start_y:
                    s.current = (i, j)
                    self._show_hit()
                    break
        self._marks_changed()

    def _search_done(self, s):
        if self.search is not s or not self.alive:
            return
        s.done = True
        if s.jump and s.current is None and s.pages:
            # Only matches above the starting point: wrap around to them.
            s.current = (s.start, 0) if s.start in s.pages else s.ordered()[0]
            self._show_hit()
        self._marks_changed()

    def _search_failed(self, s, message):
        if self.search is s:
            s.error, s.done = message, True
            sublime.status_message("PDF Viewer: search failed: %s" % message)
            self._marks_changed()

    def _hit_box(self, i, hit):
        xs = [c for q in hit.quads for c in q[0::2]]
        ys = [c for q in hit.quads for c in q[1::2]]
        x0, y0 = self._layout_point(i, min(xs), min(ys))
        x1, y1 = self._layout_point(i, max(xs), max(ys))
        return x0, y0, x1, y1

    def _show_hit(self):
        """Scroll the selected match into view, a third of the way down if it was hidden."""
        s = self.search
        if not s or s.current is None or not self.alive:
            return
        i, j = s.current

        def target():
            x0, y0, x1, y1 = self._hit_box(i, s.pages[i][j])
            vx, vy = self.view.viewport_position()
            vw, vh = self.view.viewport_extent()
            if y0 < vy or y1 > vy + vh:
                vy = max(0.0, y0 - vh / 3.0)
            if x0 < vx or x1 > vx + vw:
                vx = max(0.0, x0 - vw / 3.0)
            return vx, vy
        self._scroll(i, target)

    def next_match(self, forward=True):
        s = self.search
        order = s.ordered() if s else []
        if not order:
            return
        if s.current in order:
            idx = (order.index(s.current) + (1 if forward else -1)) % len(order)
        else:
            # Nothing selected yet: start from the viewport top.
            vy = self.view.viewport_position()[1]
            below = [n for n, (p, j) in enumerate(order) if self._hit_box(p, s.pages[p][j])[1] >= vy]
            if forward:
                idx = below[0] if below else 0
            else:
                idx = below[0] - 1 if below else len(order) - 1
        self.select_match(order[idx])

    def select_match(self, ref):
        if self.search:
            self.search.current = ref
            self._marks_changed()
            self._show_hit()

    def clear_search(self):
        if self.search is not None:
            self.search = None
            self._marks_changed()

    def _marks_changed(self):
        self.marks_cache = {}
        self.last_sig = None
        if self.pages and self.alive and self.view.is_valid():
            self._update_visible()
            self._prune()

    def _band_key(self, i, k):
        """Cache key of strip k of page i as it should look now, and its search marks."""
        digest, marks = self._marks(i, k)
        return (self.dpi, self.invert, i, k, digest), marks

    def _marks(self, i, k):
        """(digest, marks) of the search matches and the selection that overlap strip k
        of page i."""
        cached = self.marks_cache.get((i, k))
        if cached is not None:
            return cached
        y0, y1 = self.bands[i][k], self.bands[i][k + 1]
        oy = self.origins[i][1]
        marks = []

        def add(quads, rgb, alpha):
            for q in quads:
                if max(q[1::2]) - oy >= y0 and min(q[1::2]) - oy <= y1:
                    marks.append({"quad": q, "color": rgb, "alpha": alpha})

        s = self.search
        for j, hit in enumerate(s.pages.get(i, ()) if s else ()):
            add(hit.quads, *(CURRENT_MARK if s.current == (i, j) else MATCH_MARK))
        if self.selection:
            add(self._selection_quads(i), *SELECT_MARK)
        marks = tuple(marks)
        digest = hashlib.sha1(repr(marks).encode("utf-8")).hexdigest()[:12] if marks else ""
        cached = self.marks_cache[(i, k)] = (digest, marks)
        return cached

    # -- selection and links (mouse) -----------------------------------------

    def select(self, anchor, head):
        """Select the text from anchor to head, each (page, [x, y]) in page space."""
        old = self.selection
        if old and old.anchor == anchor and old.head == head:
            return
        new = Selection(anchor, head)
        if old and old.anchor == anchor:
            new.quads = old.quads   # each with its range: stand-ins until the new ones come
        self._selection_replaced(old, new)

    def select_unit(self, i, x, y, unit):
        """Select the word or the line at (x, y) on page i (double and triple click)."""

        def work():
            try:
                r = self.query.call({"op": "snap", "page": i, "p": [x, y], "unit": unit})
            except Exception as e:
                log("snap on page", i + 1, "failed:", e)
                return
            if r.get("a"):
                sublime.set_timeout(lambda: self.alive and self.select((i, r["a"]), (i, r["b"])), 0)

        threading.Thread(target=work, daemon=True).start()

    def clear_selection(self):
        if self.selection:
            self._selection_replaced(self.selection, None)

    def _selection_replaced(self, old, new):
        self.selection = new
        changed = {i for i in self.window_pages
                   if (old.range_for(i) if old else None) != (new.range_for(i) if new else None)}
        if changed and self.view.is_valid():
            self.marks_cache = {pk: m for pk, m in self.marks_cache.items() if pk[0] not in changed}
            self._update_visible()
            self._prune()

    def _selection_quads(self, i):
        """Quads of the selected text on page i. Until the helper has them for the current
        range, the ones for an earlier range stand in."""
        sel = self.selection
        rng = sel.range_for(i)
        if rng is None:
            return ()
        have = sel.quads.get(i)
        if have is None or have[0] != rng:
            with self.sel_lock:
                if self.sel_requests.get(i) != rng and self.sel_busy != (i, rng):
                    self.sel_requests[i] = rng
                    if self.sel_thread is None:
                        self.sel_thread = threading.Thread(target=self._selection_worker,
                                                           name="pdf-select", daemon=True)
                        self.sel_thread.start()
                    self.sel_lock.notify_all()
        return have[1] if have else ()

    def _selection_worker(self):
        while True:
            with self.sel_lock:
                while self.alive and not self.sel_requests:
                    self.sel_lock.wait()
                if not self.alive:
                    return
                i, rng = self.sel_requests.popitem()   # the newest request first
                self.sel_busy = (i, rng)
            try:
                quads = self.query.call({"op": "select", "page": i, "a": rng[0], "b": rng[1]})["quads"]
            except Exception as e:
                log("selection on page", i + 1, "failed:", e)
                quads = []
            with self.sel_lock:
                self.sel_busy = None
            sublime.set_timeout(lambda i=i, rng=rng, quads=quads: self._quads_ready(i, rng, quads), 0)

    def _quads_ready(self, i, rng, quads):
        sel = self.selection
        if not self.alive or not sel or sel.range_for(i) != rng or not self.view.is_valid():
            return
        sel.quads[i] = (rng, quads)
        self.marks_cache = {pk: m for pk, m in self.marks_cache.items() if pk[0] != i}
        self._update_visible()
        self._prune()

    def copy_selection(self):
        sel = self.selection
        if not sel:
            sublime.status_message("PDF Viewer: nothing selected (drag over the text to select it)")
            return
        p0, a, p1, b = sel.span()
        if p1 - p0 > 10:
            sublime.status_message("PDF Viewer: copying %d pages…" % (p1 - p0 + 1))

        def work():
            parts = []
            try:
                for i in range(p0, p1 + 1):
                    start, end = sel.range_for(i)
                    parts.append(self.query.call({"op": "copy", "page": i, "a": start, "b": end})["text"])
            except Exception as e:
                sublime.set_timeout(lambda e=e: sublime.status_message("PDF Viewer: copy failed: %s" % e), 0)
                return
            # Code listings indent with no-break spaces, which compilers reject.
            text = "\n".join(part.rstrip("\n") for part in parts).replace("\u00a0", " ")

            def done():
                sublime.set_clipboard(text)
                sublime.status_message("PDF Viewer: copied %d characters" % len(text))

            sublime.set_timeout(done, 0)

        threading.Thread(target=work, daemon=True).start()

    def click(self, i, x, y):
        """A plain click at (x, y) on page i: follow the link there, if there is one."""

        def work():
            links = self.links.get(i)
            if links is None:
                try:
                    links = self.links[i] = self.query.call({"op": "links", "page": i})["links"]
                except Exception as e:
                    log("links on page", i + 1, "failed:", e)
                    return
            for link in links:
                x0, y0, x1, y1 = link["rect"]
                if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
                    sublime.set_timeout(lambda link=link: self.follow(link), 0)
                    return

        threading.Thread(target=work, daemon=True).start()

    def follow(self, link):
        if not self.alive or not self.view.is_valid():
            return
        uri = link["uri"]
        if link["external"]:
            if re.match(r"(?i)(https?|ftp|mailto):", uri):
                webbrowser.open_new_tab(uri)
                sublime.status_message("PDF Viewer: opened %s" % uri)
            else:
                sublime.status_message("PDF Viewer: link to %s" % uri)
            return
        if 0 <= link["page"] < len(self.pages):
            self.history.append(self._position())
            del self.future[:]
            self.scroll_to(link["page"], link["y"])
            sublime.status_message("PDF Viewer: page %d (alt+left goes back)" % (link["page"] + 1))

    def history_step(self, forward=False):
        """Go back to where a link was followed from, or forward again."""
        src, dst = (self.future, self.history) if forward else (self.history, self.future)
        if not src:
            sublime.status_message("PDF Viewer: nothing to go %s to" % ("forward" if forward else "back"))
            return
        dst.append(self._position())
        self._restore(src.pop())
        self.last_sig = None
        self._update_visible()

    # -- text ----------------------------------------------------------------

    def get_text_pages(self):
        if self.text_pages is None:
            self.text_pages = pdf_text_pages(self.path)
        return self.text_pages

    def get_outline(self):
        if self.outline is None:
            self.outline = self.query.call({"op": "outline"})["items"]
        return self.outline


# --------------------------------------------------------------------------
# opening / converting views

def find_viewer(window, path):
    for v in window.views():
        if v.settings().get(VIEWER_PATH) == path and v.id() in _docs:
            return v
    return None


def create_viewer(window, path, group=None, index=None):
    view = window.new_file()
    view.set_name(os.path.basename(path))
    view.set_scratch(True)
    s = view.settings()
    s.set(IS_VIEWER, True)
    s.set(VIEWER_PATH, path)
    for k, val in VIEW_SETTINGS.items():
        s.set(k, val)
    apply_color_scheme(view, viewer_color_scheme(view))
    view.set_read_only(True)
    if group is not None and index is not None:
        window.set_view_index(view, group, index)
    window.focus_view(view)
    attach(view, path)
    return view


def attach(view, path):
    old = _docs.pop(view.id(), None)
    if old:
        old.close(delete_cache=False)
    start_mouse()
    doc = PdfDoc(view, path)
    _docs[view.id()] = doc
    doc.start()
    return doc


def convert_hex_view(hex_view):
    _pending.discard(hex_view.id())
    window = hex_view.window()
    path = hex_view.file_name()
    if not window or not hex_view.is_valid() or not path:
        return
    if hex_view.is_loading():
        _pending.add(hex_view.id())
        sublime.set_timeout(lambda: convert_hex_view(hex_view), 50)
        return
    group, index = window.get_view_index(hex_view)
    existing = find_viewer(window, path)
    hex_view.close()
    if existing:
        window.focus_view(existing)
        return
    create_viewer(window, path, group, index)


def maybe_convert(view):
    if view.id() in _pending or view.id() in _docs:
        return
    path = view.file_name()
    if not is_pdf_path(path) or not os.path.isfile(path):
        return
    if view.settings().get(RAW_MARK) or not settings().get("enabled", True):
        return
    _pending.add(view.id())
    sublime.set_timeout(lambda: convert_hex_view(view), 0)


class PdfViewerListener(sublime_plugin.EventListener):

    def on_load(self, view):
        maybe_convert(view)

    def on_activated(self, view):
        global _focus
        _focus = view.element()
        if view.file_name() and not view.is_loading():
            maybe_convert(view)

    def on_text_command(self, view, name, args):
        if name == "drag_select" and _mouse and args and "event" in args:
            # The pointer thread may see this press a moment after Sublime does.
            event, seen = args["event"], time.time()
            sublime.set_timeout(lambda: _mouse.calibrate(view, event, seen), 100)
        return None

    def on_pre_close(self, view):
        doc = _docs.pop(view.id(), None)
        if doc:
            doc.close()

    def on_query_context(self, view, key, operator, operand, match_all):
        if key not in (SEARCH_CONTEXT, SELECTION_CONTEXT):
            return None
        doc = _docs.get(view.id())
        active = bool(doc and (doc.search if key == SEARCH_CONTEXT else doc.selection))
        if operator == sublime.OP_EQUAL:
            return active == bool(operand)
        if operator == sublime.OP_NOT_EQUAL:
            return active != bool(operand)
        return None


# --------------------------------------------------------------------------
# commands

class _ViewerCommand(sublime_plugin.TextCommand):

    @property
    def doc(self):
        return _docs.get(self.view.id())

    def is_enabled(self):
        return self.view.id() in _docs

    def is_visible(self):
        return self.view.id() in _docs


class PdfViewerZoomCommand(_ViewerCommand):

    def run(self, edit, delta=None, mode=None, zoom=None):
        doc = self.doc
        if not doc or not doc.pages:
            return
        if mode:
            doc.set_zoom(mode)
        elif zoom is not None:
            doc.set_zoom(float(zoom))
        elif delta:
            doc.zoom_by(float(delta))


class PdfViewerGotoPageCommand(_ViewerCommand):

    def run(self, edit, page=None, delta=None):
        doc = self.doc
        if not doc or not doc.pages:
            return
        n = len(doc.pages)
        if delta is not None:
            cur = doc.current_page()
            vy = doc.view.viewport_position()[1]
            if delta < 0 and vy > doc._row_y(cur) + 2:
                delta += 1   # first go to the top of the current page
            doc.goto_page(cur + int(delta))
            return
        if page is not None:
            page = int(page)
            doc.goto_page(n + page if page < 0 else page - 1)
            return
        window = self.view.window()
        if not window:
            return

        def on_done(text):
            try:
                target = int(text.strip())
            except ValueError:
                sublime.status_message("PDF Viewer: not a page number")
                return
            target = n + target if target < 0 else target - 1
            sublime.set_timeout(lambda: doc.goto_page(target), 30)

        window.show_input_panel("Go to page (1-%d, currently %d):" % (n, doc.current_page() + 1), "",
                                on_done, None, None)


class PdfViewerFindCommand(_ViewerCommand):
    last_query = ""

    def run(self, edit):
        doc = self.doc
        window = self.view.window()
        if not doc or not doc.pages or not window:
            return
        if doc.engine != "mupdf":
            self._find_lines(doc, window)
            return
        start, start_y = doc.current_page(), self.view.viewport_position()[1]
        pos = doc._position()
        state = {"text": None, "open": True}

        def search_if_unchanged(text):
            if state["open"] and state["text"] == text:
                doc.find(text, start, start_y)

        def on_change(text):
            # Search as you type, once typing pauses.
            state["text"] = text
            sublime.set_timeout(lambda: search_if_unchanged(text), 250)

        def on_done(text):
            state["open"] = False
            PdfViewerFindCommand.last_query = text
            if doc.search and doc.search.query == text.strip():
                sublime.set_timeout(doc._show_hit, 120)   # the closing panel can swallow the scroll
            else:
                doc.find(text, start, start_y)

        def on_cancel():
            state["open"] = False
            doc.clear_search()
            sublime.set_timeout(lambda: doc._restore(pos), 50)

        panel = window.show_input_panel("Find in PDF:", PdfViewerFindCommand.last_query,
                                        on_done, on_change, on_cancel)
        panel.run_command("select_all")

    def _find_lines(self, doc, window):
        """Without mutool: list the text lines that contain the query, by page."""

        def on_done(query):
            query = query.strip()
            if not query:
                return
            PdfViewerFindCommand.last_query = query
            self.view.set_status(STATUS_KEY, "PDF: searching…")
            threading.Thread(target=self._search_lines, args=(doc, window, query), daemon=True).start()

        panel = window.show_input_panel("Find in PDF:", PdfViewerFindCommand.last_query,
                                        on_done, None, None)
        panel.run_command("select_all")

    def _search_lines(self, doc, window, query):
        try:
            pages = doc.get_text_pages()
        except Exception as e:
            sublime.set_timeout(lambda e=e: sublime.error_message("PDF Viewer: %s" % e), 0)
            return
        needle = query.lower()
        hits = []
        for i, text in enumerate(pages):
            for line in text.splitlines():
                if needle in line.lower():
                    hits.append((i, " ".join(line.split())))
                    if len(hits) >= 500:
                        break
            if len(hits) >= 500:
                break

        def show():
            if not doc.alive:
                return
            doc.last_sig = None
            doc._update_visible()
            if not hits:
                sublime.status_message("PDF Viewer: no matches for %r" % query)
                return
            items = [[snippet[:120] or "(match)", "Page %d" % (p + 1)] for p, snippet in hits]

            def on_select(idx):
                if idx >= 0:
                    doc.goto_page(hits[idx][0])

            def on_highlight(idx):
                if idx >= 0:
                    doc.goto_page(hits[idx][0], animate=False)

            window.show_quick_panel(items, on_select, 0, 0, on_highlight)

        sublime.set_timeout(show, 0)


class PdfViewerFindNextCommand(_ViewerCommand):

    def run(self, edit, forward=True):
        doc = self.doc
        if not doc or not doc.pages:
            return
        if doc.search:
            doc.next_match(forward)
        elif doc.engine == "mupdf" and PdfViewerFindCommand.last_query.strip():
            doc.find(PdfViewerFindCommand.last_query)
        else:
            self.view.run_command("pdf_viewer_find")


class PdfViewerClearSearchCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if doc:
            doc.clear_search()


class PdfViewerListMatchesCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        window = self.view.window()
        s = doc.search if doc else None
        if not s or not s.total() or not window:
            sublime.status_message("PDF Viewer: no matches to list")
            return
        order = s.ordered()
        pos = doc._position()
        items = [sublime.QuickPanelItem(" ".join(s.pages[p][j].text.split()) or s.query,
                                        annotation="p. %d" % (p + 1))
                 for p, j in order]

        def on_select(idx):
            if doc.search is not s:
                return
            if idx < 0:
                doc._restore(pos)
            else:
                doc.select_match(order[idx])

        def on_highlight(idx):
            if doc.search is s and idx >= 0:
                doc.select_match(order[idx])

        selected = order.index(s.current) if s.current in order else 0
        window.show_quick_panel(items, on_select, 0, selected, on_highlight)


class PdfViewerOutlineCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        window = self.view.window()
        if not doc or not doc.pages or not window:
            return
        if doc.engine != "mupdf":
            sublime.status_message("PDF Viewer: the outline needs mutool (mupdf-tools)")
            return

        def work():
            try:
                items = doc.get_outline()
            except Exception as e:
                sublime.set_timeout(lambda e=e: sublime.status_message("PDF Viewer: %s" % e), 0)
                return
            sublime.set_timeout(lambda: self._show(doc, window, items), 0)

        threading.Thread(target=work, daemon=True).start()

    def _show(self, doc, window, items):
        if not doc.alive:
            return
        entries = [it for it in items if 0 <= it["page"] < len(doc.pages)]
        if not entries:
            sublime.status_message("PDF Viewer: this PDF has no outline")
            return
        pos = doc._position()
        current = doc.current_page()
        selected = 0
        for idx, it in enumerate(entries):
            if it["page"] <= current:
                selected = idx
        panel_items = [sublime.QuickPanelItem("\u2003" * it["depth"] + it["title"],
                                              annotation="p. %d" % (it["page"] + 1))
                       for it in entries]

        def on_select(idx):
            if idx < 0:
                doc._restore(pos)
            else:
                doc.scroll_to(entries[idx]["page"], entries[idx]["y"])

        def on_highlight(idx):
            if idx >= 0:
                doc.scroll_to(entries[idx]["page"], entries[idx]["y"])

        window.show_quick_panel(panel_items, on_select, 0, selected, on_highlight)


class PdfViewerCopyCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if doc:
            doc.copy_selection()


class PdfViewerClearSelectionCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if doc:
            doc.clear_selection()


class PdfViewerHistoryCommand(_ViewerCommand):

    def run(self, edit, forward=False):
        doc = self.doc
        if doc and doc.pages:
            doc.history_step(forward)


class PdfViewerScrollCommand(_ViewerCommand):

    def run(self, edit, lines=3):
        doc = self.doc
        if doc and doc.pages:
            doc.scroll_by(lines * self.view.line_height())


class PdfViewerShowTextCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        window = self.view.window()
        if not doc or not window:
            return

        def work():
            try:
                pages = doc.get_text_pages()
            except Exception as e:
                sublime.set_timeout(lambda e=e: sublime.error_message("PDF Viewer: %s" % e), 0)
                return
            text = ("\n\n" + "-" * 78 + "\n\n").join(
                "[Page %d]\n\n%s" % (i + 1, p.rstrip()) for i, p in enumerate(pages))

            def show():
                v = window.new_file()
                v.set_name(os.path.basename(doc.path) + " (text)")
                v.set_scratch(True)
                v.run_command("append", {"characters": text})
                v.sel().clear()
                v.sel().add(sublime.Region(0))

            sublime.set_timeout(show, 0)

        threading.Thread(target=work, daemon=True).start()


class PdfViewerOpenExternalCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if not doc:
            return
        try:
            if sys.platform.startswith("darwin"):
                subprocess.Popen(["open", doc.path])
            elif sys.platform == "win32":
                os.startfile(doc.path)  # noqa
            else:
                subprocess.Popen(["xdg-open", doc.path])
        except Exception as e:
            sublime.error_message("PDF Viewer: could not open externally: %s" % e)


class PdfViewerOpenRawCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        window = self.view.window()
        if not doc or not window:
            return
        group, index = window.get_view_index(self.view)
        raw = window.open_file(doc.path)
        raw.settings().set(RAW_MARK, True)
        window.set_view_index(raw, group, index)
        self.view.close()
        window.focus_view(raw)


class PdfViewerToggleInvertCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if doc and doc.pages:
            doc.toggle_invert()


class PdfViewerReloadCommand(_ViewerCommand):

    def run(self, edit):
        doc = self.doc
        if not doc:
            return
        pos = doc._position() if doc.pages else None
        mode, invert = doc.zoom_mode, doc.invert
        doc.close(delete_cache=True)
        new = attach(self.view, doc.path)
        new.zoom_mode, new.invert = mode, invert
        if pos:
            sublime.set_timeout(lambda: new._restore(pos), 400)


HELP_HTML = """
<body id="pdf-viewer-help">
<style>
  body { padding: 0.6rem 1rem; }
  h3 { margin: 0 0 0.5rem 0; }
  .k { font-weight: bold; font-family: monospace; }
  div { margin: 0.1rem 0; }
</style>
<h3>PDF Viewer</h3>
<div><span class="k">+ / -</span> zoom in / out</div>
<div><span class="k">0 or w</span> fit width &nbsp; <span class="k">f</span> fit page &nbsp; <span class="k">1</span> 100%</div>
<div><span class="k">space / n</span> next page &nbsp; <span class="k">shift+space / p</span> previous page</div>
<div><span class="k">j / k, arrows</span> scroll &nbsp; <span class="k">g</span> go to page &nbsp; <span class="k">home / end</span></div>
<div><span class="k">/ or ctrl+f</span> find &nbsp; <span class="k">alt+enter</span> list matches &nbsp; <span class="k">escape</span> clear</div>
<div><span class="k">enter or F3</span> next match &nbsp; <span class="k">shift+enter or shift+F3</span> previous match</div>
<div><span class="k">o or ctrl+r</span> outline &nbsp; <span class="k">t</span> show extracted text</div>
<div><span class="k">drag</span> select text &nbsp; <span class="k">double / triple click</span> word / line &nbsp; <span class="k">shift+click</span> extend</div>
<div><span class="k">ctrl+c</span> copy &nbsp; <span class="k">click a link</span> follow it &nbsp; <span class="k">alt+left / alt+right</span> back / forward</div>
<div><span class="k">i</span> invert colors &nbsp; <span class="k">r</span> reload</div>
<div><span class="k">e</span> open in external viewer &nbsp; <span class="k">h</span> open raw (hex)</div>
</body>
"""


class PdfViewerHelpCommand(_ViewerCommand):

    def run(self, edit):
        self.view.show_popup(HELP_HTML, sublime.HIDE_ON_MOUSE_MOVE_AWAY,
                             self.view.text_point(0, 0), 760, 440)


class PdfViewerOpenCommand(sublime_plugin.WindowCommand):

    def run(self, path=None):
        if path:
            self._open(path)
            return
        view = self.window.active_view()
        initial = ""
        if view and view.file_name():
            initial = os.path.dirname(view.file_name()) + os.sep
        elif self.window.folders():
            initial = self.window.folders()[0] + os.sep
        self.window.show_input_panel("Open PDF:", initial, self._open, None, None)

    def _open(self, path):
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isfile(path):
            sublime.error_message("PDF Viewer: no such file\n%s" % path)
            return
        existing = find_viewer(self.window, path)
        if existing:
            self.window.focus_view(existing)
        else:
            create_viewer(self.window, path)


# --------------------------------------------------------------------------
# mouse (X11)
#
# Sublime doesn't tell plugins about clicks on phantoms. On X11 any program may watch the
# mouse buttons and ask where the pointer is, so a thread does that for Sublime's own
# windows, and the main thread maps the pointer onto the pages.

XI_RAW_PRESS, XI_RAW_RELEASE = 15, 16   # XInput2 raw button events
XI_ALL_MASTER_DEVICES = 1
GENERIC_EVENT = 35
SHIFT_MASK, BUTTON1_MASK = 0x1, 0x100
OVERLAYS = ("command_palette:input", "goto_anything:input", "quick_panel:input")
DOUBLE_CLICK = 0.4   # seconds between the presses of a double click
DRAG_SLOP = 3        # pixels the pointer moves before a press becomes a drag
SCROLLBAR = 12       # pixels at the right (and bottom) edge of a view left to its scroll bars
MENU_HEIGHT = 25     # menu bar height, until a click on text measures it

_focus = None    # element() of the view with the focus: clicks belong to an open overlay
_watch = None    # PointerWatch, False where the mouse can't be watched
_mouse = None    # Mouse


class XIEventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int),
                ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class XGenericEventCookie(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("cookie", ctypes.c_uint), ("data", ctypes.c_void_p)]


class XIRawEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("extension", ctypes.c_int), ("evtype", ctypes.c_int),
                ("time", ctypes.c_ulong), ("deviceid", ctypes.c_int), ("sourceid", ctypes.c_int),
                ("detail", ctypes.c_int), ("flags", ctypes.c_int)]


X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class Xlib(object):
    """The few Xlib and XInput2 calls the pointer thread needs, through ctypes."""

    def __init__(self):
        C = ctypes
        self.x = x = C.CDLL("libX11.so.6")
        self.xi = xi = C.CDLL("libXi.so.6")
        x.XOpenDisplay.restype = C.c_void_p
        x.XOpenDisplay.argtypes = [C.c_char_p]
        x.XCloseDisplay.argtypes = [C.c_void_p]
        x.XDefaultRootWindow.restype = C.c_ulong
        x.XDefaultRootWindow.argtypes = [C.c_void_p]
        x.XConnectionNumber.argtypes = [C.c_void_p]
        x.XPending.argtypes = [C.c_void_p]
        x.XNextEvent.argtypes = [C.c_void_p, C.c_void_p]
        x.XFlush.argtypes = [C.c_void_p]
        x.XGetEventData.argtypes = [C.c_void_p, C.c_void_p]
        x.XFreeEventData.argtypes = [C.c_void_p, C.c_void_p]
        x.XQueryExtension.argtypes = [C.c_void_p, C.c_char_p] + [C.POINTER(C.c_int)] * 3
        x.XInternAtom.restype = C.c_ulong
        x.XInternAtom.argtypes = [C.c_void_p, C.c_char_p, C.c_int]
        x.XQueryPointer.argtypes = ([C.c_void_p, C.c_ulong] + [C.POINTER(C.c_ulong)] * 2 +
                                    [C.POINTER(C.c_int)] * 4 + [C.POINTER(C.c_uint)])
        x.XGetWindowProperty.argtypes = [C.c_void_p, C.c_ulong, C.c_ulong, C.c_long, C.c_long,
                                         C.c_int, C.c_ulong, C.POINTER(C.c_ulong),
                                         C.POINTER(C.c_int), C.POINTER(C.c_ulong),
                                         C.POINTER(C.c_ulong), C.POINTER(C.c_void_p)]
        x.XFree.argtypes = [C.c_void_p]
        x.XSetErrorHandler.restype = C.c_void_p
        x.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
        xi.XIQueryVersion.argtypes = [C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int)]
        xi.XISelectEvents.argtypes = [C.c_void_p, C.c_ulong, C.POINTER(XIEventMask), C.c_int]
        # Xlib's default error handler ends the process, and a window can disappear between
        # two calls. The handler is installed once per plugin host and kept alive on the
        # ctypes module, which plugin reloads leave alone.
        if not hasattr(C, "_pdf_viewer_x_error_handler"):
            C._pdf_viewer_x_error_handler = X_ERROR_HANDLER(lambda display, error: 0)
            x.XSetErrorHandler(C._pdf_viewer_x_error_handler)

    def listen(self, dpy, root):
        """Ask for raw button events from every pointer. Returns XInput's opcode, or None."""
        C = ctypes
        opcode, event, error = C.c_int(), C.c_int(), C.c_int()
        if not self.x.XQueryExtension(dpy, b"XInputExtension", C.byref(opcode), C.byref(event),
                                      C.byref(error)):
            return None
        major, minor = C.c_int(2), C.c_int(0)
        if self.xi.XIQueryVersion(dpy, C.byref(major), C.byref(minor)) != 0:
            return None
        bits = (C.c_ubyte * 4)()
        for evtype in (XI_RAW_PRESS, XI_RAW_RELEASE):
            bits[evtype >> 3] |= 1 << (evtype & 7)
        mask = XIEventMask(XI_ALL_MASTER_DEVICES, len(bits), C.cast(bits, C.POINTER(C.c_ubyte)))
        self.xi.XISelectEvents(dpy, root, C.byref(mask), 1)
        self.x.XFlush(dpy)
        return opcode.value

    def raw_button(self, dpy, event, opcode):
        """(event type, button) of a raw button event, None for other events."""
        cookie = XGenericEventCookie.from_buffer(event)
        if cookie.type != GENERIC_EVENT or cookie.extension != opcode:
            return None
        address = ctypes.addressof(event)
        if not self.x.XGetEventData(dpy, address):
            return None
        try:
            cookie = XGenericEventCookie.from_buffer(event)
            if cookie.evtype not in (XI_RAW_PRESS, XI_RAW_RELEASE):
                return None
            return cookie.evtype, ctypes.cast(cookie.data, ctypes.POINTER(XIRawEvent)).contents.detail
        finally:
            self.x.XFreeEventData(dpy, address)

    def pointer(self, dpy, window):
        """(child, x, y, mask): the pointer relative to window, and the child it is over."""
        C = ctypes
        root, child = C.c_ulong(), C.c_ulong()
        rx, ry, wx, wy, mask = C.c_int(), C.c_int(), C.c_int(), C.c_int(), C.c_uint()
        if not self.x.XQueryPointer(dpy, window, C.byref(root), C.byref(child), C.byref(rx),
                                    C.byref(ry), C.byref(wx), C.byref(wy), C.byref(mask)):
            return None
        return child.value, wx.value, wy.value, mask.value

    def card32(self, dpy, window, atom):
        """A 32-bit property of a window (a pid, a window id), or None."""
        C = ctypes
        kind, fmt, count, after, data = C.c_ulong(), C.c_int(), C.c_ulong(), C.c_ulong(), C.c_void_p()
        if self.x.XGetWindowProperty(dpy, window, atom, 0, 1, False, 0, C.byref(kind), C.byref(fmt),
                                     C.byref(count), C.byref(after), C.byref(data)) != 0:
            return None
        try:
            if not data.value or not count.value or fmt.value != 32:
                return None
            return C.cast(data, C.POINTER(C.c_ulong))[0]
        finally:
            if data.value:
                self.x.XFree(data)


class PointerWatch(object):
    """A thread that reports mouse button 1 over Sublime's windows: the press, the pointer
    60 times a second while the button is held, and the release. It sleeps otherwise.
    Events reach handler on the main thread as dicts, positions in the window's pixels."""

    POLL = 1.0 / 60

    def __init__(self, handler):
        self.handler = handler
        self.xlib = Xlib()   # OSError without libX11 / libXi
        self.wake_r, self.wake_w = os.pipe()
        self.stopped = False
        self.presses = 0         # presses reported so far
        self.ignored = 0         # the main thread has no use for this press: don't follow it
        self.last_press = None   # {"time", "x", "y"} of the latest press over Sublime
        self.thread = threading.Thread(target=self._run, name="pdf-pointer", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped = True
        try:
            os.write(self.wake_w, b"x")
        except OSError:
            pass

    def _post(self, **event):
        sublime.set_timeout(lambda: self.handler(event), 0)

    def _run(self):
        dpy = self.xlib.x.XOpenDisplay(None)
        if not dpy:
            print("[PDFViewer] mouse selection off: no X display")
            return
        try:
            self._loop(dpy)
        except Exception as e:
            print("[PDFViewer] mouse selection stopped:", e)
        finally:
            self.xlib.x.XCloseDisplay(dpy)
            os.close(self.wake_r)
            os.close(self.wake_w)

    def _loop(self, dpy):
        X = self.xlib
        root = X.x.XDefaultRootWindow(dpy)
        opcode = X.listen(dpy, root)
        if opcode is None:
            print("[PDFViewer] mouse selection off: the X server lacks XInput2")
            return
        atoms = {name: X.x.XInternAtom(dpy, name.encode("ascii"), False)
                 for name in ("_NET_WM_PID", "_NET_ACTIVE_WINDOW", "_NET_WM_WINDOW_TYPE",
                              "_NET_WM_WINDOW_TYPE_NORMAL", "WM_TRANSIENT_FOR")}
        fd = X.x.XConnectionNumber(dpy)
        event = ctypes.create_string_buffer(256)   # an XEvent is 192 bytes
        held = None    # the Sublime window button 1 went down in
        last = (0, 0)
        due = 0.0
        while not self.stopped:
            # Xlib may have read events off the socket already (while waiting for a reply),
            # and then select() wouldn't wake up for them.
            if not X.x.XPending(dpy):
                timeout = max(0.0, due - time.time()) if held else None
                if self.wake_r in select.select([fd, self.wake_r], [], [], timeout)[0]:
                    return
            while X.x.XPending(dpy):
                X.x.XNextEvent(dpy, event)
                raw = X.raw_button(dpy, event, opcode)
                if not raw or raw[1] != 1:
                    continue
                if raw[0] == XI_RAW_PRESS:
                    held = self._press(dpy, root, atoms)
                    due = time.time() + self.POLL
                elif held:
                    p = X.pointer(dpy, held)
                    x, y = (p[1], p[2]) if p else last
                    self._post(kind="release", x=x, y=y, mask=0)
                    held = None
            if held and time.time() >= due:
                due = time.time() + self.POLL
                p = X.pointer(dpy, held)
                if p and p[3] & BUTTON1_MASK:
                    last = (p[1], p[2])
                    if self.ignored != self.presses:
                        self._post(kind="motion", x=p[1], y=p[2], mask=p[3])
                else:   # the release went missing
                    self._post(kind="release", x=last[0], y=last[1], mask=0)
                    held = None

    def _press(self, dpy, root, atoms):
        """Report a press over one of Sublime's main windows and return that window."""
        X = self.xlib
        chain, window = [], root
        while window:
            p = X.pointer(dpy, window)
            if not p:
                return None
            chain.append((window,) + p[1:])
            window = p[0]
        for window, x, y, mask in reversed(chain[1:]):
            pid = X.card32(dpy, window, atoms["_NET_WM_PID"])
            if pid is None:
                continue
            if pid != os.getppid():   # the plugin host's parent is Sublime
                return None
            # Not its dialogs or menus, which are windows of their own.
            kind = X.card32(dpy, window, atoms["_NET_WM_WINDOW_TYPE"])
            if kind is not None and kind != atoms["_NET_WM_WINDOW_TYPE_NORMAL"]:
                return None
            if kind is None and X.card32(dpy, window, atoms["WM_TRANSIENT_FOR"]) is not None:
                return None
            active = X.card32(dpy, root, atoms["_NET_ACTIVE_WINDOW"])
            now = time.time()
            self.presses += 1
            self.last_press = {"time": now, "x": x, "y": y}
            self._post(kind="press", x=x, y=y, mask=mask, time=now, focus=_focus, seq=self.presses,
                       active=None if active is None else active == window)
            return window
        return None


class Drag(object):
    __slots__ = ("doc", "anchor", "x", "y", "moved")

    def __init__(self, doc, anchor, x, y, moved):
        self.doc = doc
        self.anchor = anchor   # (page, [x, y]) the selection grows from
        self.x, self.y = x, y  # where the press was, in window pixels
        self.moved = moved


class Mouse(object):
    """Turns the pointer thread's events into text selections and link clicks."""

    def __init__(self):
        self.drag = None
        self.last = (0.0, 0, 0, 0)   # time, x, y and click count of the latest press
        self.offsets = {}            # "menu" / "nomenu" -> (dx, dy), see calibrate()
        try:
            with open(self._offsets_file(), encoding="utf-8") as fh:
                self.offsets = {k: tuple(v) for k, v in json.load(fh).items()}
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _offsets_file():
        return os.path.join(cache_root(), "mouse.json")

    def _window_xy(self, window, x, y):
        """Sublime's window coordinates of X pixel (x, y): they start below the menu bar."""
        menu = window.is_menu_visible()
        dx, dy = self.offsets.get("menu" if menu else "nomenu", (0, MENU_HEIGHT if menu else 0))
        return x - dx, y - dy

    def calibrate(self, view, event, seen):
        """A click on text: Sublime reports it in its window coordinates, the pointer thread
        saw it in X pixels, and the difference is the menu bar above Sublime's drawing."""
        press = _watch.last_press if _watch else None
        window = view.window() if view.is_valid() else None
        if not press or not window or abs(press["time"] - seen) > 0.3:
            return
        dx, dy = int(round(press["x"] - event["x"])), int(round(press["y"] - event["y"]))
        if not (0 <= dx <= 40 and 0 <= dy <= 120):
            return
        key = "menu" if window.is_menu_visible() else "nomenu"
        if self.offsets.get(key) != (dx, dy):
            self.offsets[key] = (dx, dy)
            try:
                os.makedirs(cache_root(), exist_ok=True)
                with open(self._offsets_file(), "w", encoding="utf-8") as fh:
                    json.dump(self.offsets, fh)
            except OSError as e:
                log("could not save the menu bar height:", e)

    @staticmethod
    def _to_layout(view, wx, wy):
        """Layout position of window point (wx, wy). window_to_layout() can't be used: it
        gives (0, 0) for points outside the view, where a drag goes to scroll."""
        vx, vy = view.viewport_position()
        width, height = view.viewport_extent()
        cx, cy = view.layout_to_window((vx + width / 2.0, vy + height / 2.0))
        return vx + width / 2.0 + wx - cx, vy + height / 2.0 + wy - cy

    @staticmethod
    def _ignore(ev):
        """Tell the pointer thread not to follow this press."""
        if _watch:
            _watch.ignored = ev["seq"]

    def handle(self, event):
        try:
            getattr(self, event["kind"])(event)
        except Exception as e:
            print("[PDFViewer] mouse %s: %s" % (event["kind"], e))

    def _viewer_at(self, window, x, y):
        """(doc, layout x, layout y) of the viewer under X pixel (x, y), if there is one."""
        wx, wy = self._window_xy(window, x, y)
        for group in range(window.num_groups()):
            view = window.active_view_in_group(group)
            doc = _docs.get(view.id()) if view else None
            if not doc or not doc.pages or doc.engine != "mupdf":
                continue
            vx, vy = view.viewport_position()
            width, height = view.viewport_extent()
            lx, ly = self._to_layout(view, wx, wy)
            bottom = height - (0 if view.settings().get("word_wrap") else SCROLLBAR)
            if 0 <= lx - vx < width - SCROLLBAR and 0 <= ly - vy < bottom:
                return doc, lx, ly
        return None

    def press(self, ev):
        self.drag = None
        window = sublime.active_window()
        hit = None
        if window and ev["focus"] not in OVERLAYS and ev["active"] is not False:
            hit = self._viewer_at(window, ev["x"], ev["y"])
        if not hit or hit[0].view.is_popup_visible():
            self._ignore(ev)
            return
        doc, lx, ly = hit
        t, x, y = ev["time"], ev["x"], ev["y"]
        lt, lx0, ly0, count = self.last
        near = abs(x - lx0) <= DRAG_SLOP and abs(y - ly0) <= DRAG_SLOP
        count = count % 3 + 1 if t - lt < DOUBLE_CLICK and near else 1
        self.last = (t, x, y, count)
        if window.active_view() != doc.view or _focus is not None:
            window.focus_view(doc.view)   # also out of a panel, so that ctrl+c reaches it
        pt = doc.point_at(lx, ly)
        if pt is None:   # beside the pages
            doc.clear_selection()
            self._ignore(ev)
            return
        i, px, py = pt
        if ev["mask"] & SHIFT_MASK and doc.selection:
            anchor = doc.selection.anchor
            doc.select(anchor, (i, [px, py]))
            self.drag = Drag(doc, anchor, x, y, moved=True)
        elif count > 1:
            doc.select_unit(i, px, py, "word" if count == 2 else "line")
            self._ignore(ev)
        else:
            self.drag = Drag(doc, (i, [px, py]), x, y, moved=False)

    def motion(self, ev):
        d = self.drag
        if not d or not d.doc.alive or not d.doc.view.is_valid():
            return
        if not d.moved:
            if abs(ev["x"] - d.x) <= DRAG_SLOP and abs(ev["y"] - d.y) <= DRAG_SLOP:
                return
            d.moved = True
        doc, view = d.doc, d.doc.view
        window = view.window()
        if not window:
            return
        wx, wy = self._window_xy(window, ev["x"], ev["y"])
        # Above or below the view: scroll, faster the further away the pointer is.
        vy = view.viewport_position()[1]
        height = view.viewport_extent()[1]
        ly = self._to_layout(view, wx, wy)[1]
        if ly < vy:
            doc.scroll_by(-((vy - ly) / 2.0 + 2))
        elif ly > vy + height:
            doc.scroll_by((ly - vy - height) / 2.0 + 2)
        lx, ly = self._to_layout(view, wx, wy)
        pt = doc.point_at(lx, ly, clamp=True)
        if pt:
            doc.select(d.anchor, (pt[0], [pt[1], pt[2]]))

    def release(self, ev):
        d, self.drag = self.drag, None
        if not d or d.moved or not d.doc.alive:
            return
        # A click without dragging drops the selection and follows a link.
        d.doc.clear_selection()
        i, (px, py) = d.anchor
        d.doc.click(i, px, py)


def start_mouse():
    """Start watching the mouse, once, where that works: Linux with X11 and XInput2."""
    global _watch, _mouse
    if _watch is not None or not settings().get("mouse_selection", True):
        return
    if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
        _watch = False
        return
    try:
        _mouse = Mouse()
        _watch = PointerWatch(_mouse.handle)
    except OSError as e:
        print("[PDFViewer] mouse selection off:", e)
        _watch = False
        return
    _watch.start()


# --------------------------------------------------------------------------

def prune_cache(max_age_hours=24):
    """Remove render caches left behind by crashed or killed sessions."""
    root = cache_root()
    try:
        entries = os.listdir(root)
    except OSError:
        return
    live = {d.cache_dir for d in _docs.values()}
    cutoff = time.time() - max_age_hours * 3600
    for name in entries:
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and path not in live and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def plugin_loaded():
    global _helper_js
    try:
        _helper_js = find_helper_js()
    except Exception as e:
        print("[PDFViewer] could not find the mutool helper:", e)
    sublime.set_timeout_async(prune_cache, 5000)
    # Re-attach to viewer views that survived a plugin reload or a session restore.
    for window in sublime.windows():
        for view in window.views():
            path = view.settings().get(VIEWER_PATH)
            if view.settings().get(IS_VIEWER) and path and view.id() not in _docs:
                if os.path.isfile(path):
                    if view.settings().get(STATE_KEY) is None:
                        # Opened by a version that kept no state: its status bar still
                        # names the page it showed.
                        m = re.match(r"PDF\s+(\d+)\s*/", view.get_status(STATUS_KEY) or "")
                        if m:
                            view.settings().set(STATE_KEY, {"page": int(m.group(1)) - 1, "frac": 0.0})
                    attach(view, path)
                else:
                    view.close()


def plugin_unloaded():
    if _watch:
        _watch.stop()
    for doc in list(_docs.values()):
        doc.close(delete_cache=True)
    _docs.clear()
