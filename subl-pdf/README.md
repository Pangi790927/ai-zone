# PDFViewer for Sublime Text

Open a `.pdf` in Sublime Text and see the rendered pages, not a hex dump.
Pages are rasterised by MuPDF (`mutool`), or by poppler's `pdftoppm` as a
fallback, and shown as a continuous, scrollable stack of images in a read-only
tab, the way evince or a browser would show them.


## Features

- Click a PDF in the sidebar (or `subl file.pdf`) and it opens rendered.
- Continuous scrolling, fit width / fit page / free zoom, re-fits when the
  window or sidebar changes size. While a new zoom renders, the old pages are
  stretched in its place instead of going blank.
- Lazy rendering: only pages near the viewport are rasterised, so 1000-page
  documents open instantly. Pages are cut into strips, and the visible ones
  come first.
- Select text with the mouse like in evince: drag, double-click a word,
  triple-click a line, shift+click to extend; `ctrl+c` copies. Links work:
  internal ones jump (`alt+←` goes back), web links open in the browser.
  Linux with X11 only, see below.
- Search as you type (`/`): every match is highlighted on the page, the
  current one in orange; step through them with `enter` / `F3`, or list
  them all with `alt+enter`.
- Outline (bookmarks) in a quick panel (`o` or `ctrl+r`), with preview.
- Extracted text in a normal editable tab (`t`).
- Inverted colours for dark themes (`i`); hues are kept.
- Escape hatches: open in the system viewer (`e`) or as raw bytes (`h`).
- A tab comes back on the same page and zoom after a restart of Sublime.

## Requirements

Sublime Text 4 and one of:

- `mupdf-tools` (`mutool`), plus `stdbuf` from coreutils: the full
  experience, including search highlights, the outline, selection and links.
- `poppler-utils` (`pdfinfo`, `pdftoppm`, `pdftotext`): rendering only;
  search lists matching lines instead of highlighting them. Also used for
  `t` (extracted text) when present.

Debian/Ubuntu: `sudo apt install mupdf-tools poppler-utils`
macOS: `brew install mupdf poppler` (without `stdbuf`, i.e. GNU coreutils'
`gstdbuf` under that name, the poppler fallback is used)

## Install

Sublime loads packages from its `Packages` directory
(`~/.config/sublime-text/Packages` on Linux, or Preferences ▸ Browse
Packages…). Link this folder in there under the name `PDFViewer`, so that
edits here take effect at once:

```sh
cd path/to/subl-pdf
ln -s "$PWD" ~/.config/sublime-text/Packages/PDFViewer
```

Copying the folder there under that name works too. The name matters: the
menu and Command Palette entries for the settings and key bindings look for
`Packages/PDFViewer`. Sublime picks the package up without a restart; install
the tools listed under Requirements first.

## Keys (inside a PDF tab)

| Key                      | Action                                |
|--------------------------|---------------------------------------|
| `+` `=` `ctrl+=`         | zoom in                               |
| `-` `ctrl+-`             | zoom out                              |
| `0` `w` `ctrl+0`         | fit width                             |
| `f`                      | fit page                              |
| `1`                      | 100 %                                 |
| `space` `n` `→`          | next page                             |
| `shift+space` `p` `←`    | previous page                         |
| `j` `k` `↓` `↑`, wheel   | scroll                                |
| `g` `ctrl+g`             | go to page                            |
| `home` `end`             | first / last page                     |
| drag                     | select text                           |
| double / triple click    | select a word / a line                |
| `shift`+click            | extend the selection                  |
| `ctrl+c`                 | copy the selected text                |
| click a link             | follow it                             |
| `alt+←` `alt+→`          | back / forward after following a link |
| `/` `ctrl+f`             | find (highlights matches as you type) |
| `enter` `F3`             | next match                            |
| `shift+enter` `shift+F3` | previous match                        |
| `alt+enter`              | list all matches                      |
| `escape`                 | clear the selection, then the search  |
| `o` `ctrl+r`             | outline (bookmarks)                   |
| `t`                      | show extracted text                   |
| `i`                      | invert colours                        |
| `r`                      | reload file                           |
| `e`                      | open in external viewer               |
| `h`                      | open raw bytes (hex) instead          |
| `?`                      | this list                             |

Everything is also in the Command Palette under `PDF Viewer:`.

## Settings

`Preferences ▸ Package Settings ▸ PDF Viewer ▸ Settings`

```js
{
    "enabled": true,            // intercept .pdf files; false = use "PDF Viewer: Open PDF…" manually
    "default_zoom": "fit_width", // "fit_width", "fit_page" or a number (1.0 = 96 dpi)
    "margin": 16,
    "page_gap": 10,
    "prerender_pages": 3,       // pages kept rendered above/below the viewport
    "render_threads": 3,        // mutool processes rendering in parallel
    "max_render_dpi": 400,
    "render_scale": 1.0,        // 2.0 on HiDPI screens for crisper pages
    "renderer": "auto",         // "auto" | "mutool" | "pdftoppm"
    "mouse_selection": true,    // select text and follow links with the mouse (X11)
    "tool_paths": {},           // e.g. {"mutool": "/opt/mupdf/bin/mutool"}
}
```

## How it works

Sublime can't draw vector graphics in a tab, only PNG, JPEG and GIF images
inside phantoms, so pages are rasterised on the CPU, as evince does with
poppler. For each open PDF the plugin starts a few `mutool run
mupdf_helper.js file.pdf` processes. They keep the document open, cache the
display lists of recently used pages, and render a page, or a strip of a tall
page, at exactly the pixel size it is shown at. A query process answers
searches and outline requests.

The tab itself is a scratch buffer with one empty line per page. Each line
carries a block phantom holding that page's strips as inline `data:` images.
Images loaded from files would flash Sublime's broken-image icon for a frame
every time a phantom is rebuilt. Rendered strips are also cached under
`~/.cache/sublime-pdf-viewer/` and removed when the tab closes. Strips are at
most 256 pt tall, so a change to the highlights only redraws the strips it
touches, and only phantoms whose page changed are replaced.

Sublime never tells plugins about mouse clicks on phantoms. On X11 any program
may watch the mouse, so a thread in the plugin listens for button presses
(XInput2 raw events, through ctypes; it sleeps otherwise) and, while the button
is held over one of Sublime's windows, asks X where the pointer is 60 times a
second. The plugin maps that onto the page, MuPDF works out the selected text
in reading order, and the selection is drawn into the page like search
highlights. Sublime's window coordinates start below its menu bar; the plugin
learns that height from the first click on ordinary text and keeps it in
`~/.cache/sublime-pdf-viewer/mouse.json`. Elsewhere the mouse does nothing on
the pages: Wayland doesn't let one program see the pointer over another's
window, and macOS and Windows would need code of their own.

Sublime draws the phantoms itself; with `"hardware_acceleration": "opengl"`
in its Preferences (a restart is needed) that drawing, and scrolling, happens
on the GPU.

Limitations: the pointer stays an arrow over pages, not a text cursor; selecting doesn't fill the middle-click (primary) selection, use
`ctrl+c`; copied text has no-break spaces turned into spaces (spec code
listings use them for indentation). Annotations are drawn but not
interactive.
