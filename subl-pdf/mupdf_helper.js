// Rendering and search helper for the PDF Viewer Sublime Text package.
//
//     mutool run mupdf_helper.js file.pdf
//
// Reads JSON requests on stdin and writes one JSON reply per line on stdout. The
// document stays open between requests, and recently used pages keep their display
// lists and text, so drawing a page again at another zoom, or following a selection
// as the mouse moves, skips re-parsing it.

var doc = null, openError = null;
try {
    doc = Document.openDocument(scriptArgs[0]);
    if (doc.needsPassword())
        openError = "the PDF is password protected";
} catch (e) {
    openError = String(e);
}

var lists = [];      // [{page, list}], most recently used last
var MAX_LISTS = 8;

function displayList(page) {
    for (var i = 0; i < lists.length; i++) {
        if (lists[i].page === page) {
            var entry = lists.splice(i, 1)[0];
            lists.push(entry);
            return entry.list;
        }
    }
    var list = doc.loadPage(page).toDisplayList(true);
    lists.push({page: page, list: list});
    if (lists.length > MAX_LISTS)
        lists.shift();
    return list;
}

var texts = [];      // [{page, text, lines}], most recently used last
var MAX_TEXTS = 8;

// A page's structured text and its characters by line: [[{c, quad}]].
function pageText(page) {
    for (var i = 0; i < texts.length; i++) {
        if (texts[i].page === page) {
            var entry = texts.splice(i, 1)[0];
            texts.push(entry);
            return entry;
        }
    }
    var text = doc.loadPage(page).toStructuredText("preserve-whitespace"), lines = [];
    text.walk({
        beginLine: function () { lines.push([]); },
        onChar: function (c, origin, font, size, quad) { lines[lines.length - 1].push({c: c, quad: quad}); },
    });
    var added = {page: page, text: text, lines: lines.filter(function (l) { return l.length; })};
    texts.push(added);
    if (texts.length > MAX_TEXTS)
        texts.shift();
    return added;
}

// The point u of the way across character quad q and v of the way down it. A selection
// starts before a character when it starts in its left half, and ends after it when it
// ends in its right half, so points a quarter in pick out whole characters.
function inQuad(q, u, v) {
    return [q[0] + (q[2] - q[0]) * u + (q[4] - q[0]) * v,
            q[1] + (q[3] - q[1]) * u + (q[5] - q[1]) * v];
}

// A selection end: a point, or "start" / "end" for the page's first / last character.
function endPoint(t, p) {
    if (p !== "start" && p !== "end")
        return p;
    var line = t.lines[p === "start" ? 0 : t.lines.length - 1];
    if (!line)
        return null;
    return p === "start" ? inQuad(line[0].quad, 0.25, 0.5) : inQuad(line[line.length - 1].quad, 0.75, 0.5);
}

// Distance from point p to the box around quad q (0 inside it).
function quadDistance(p, q) {
    var xs = [q[0], q[2], q[4], q[6]], ys = [q[1], q[3], q[5], q[7]];
    var dx = Math.max(Math.min.apply(null, xs) - p[0], 0, p[0] - Math.max.apply(null, xs));
    var dy = Math.max(Math.min.apply(null, ys) - p[1], 0, p[1] - Math.max.apply(null, ys));
    return Math.sqrt(dx * dx + dy * dy);
}

var OPENING = "([{<\"'\u2018\u201c\u00ab";
var CLOSING = ".,;:!?)]}>\"'\u2019\u201d\u00bb";

function quadPath(q) {
    // MuPDF quads are [ul, ur, ll, lr]; walk them around the edge.
    var path = new Path();
    path.moveTo(q[0], q[1]);
    path.lineTo(q[2], q[3]);
    path.lineTo(q[6], q[7]);
    path.lineTo(q[4], q[5]);
    path.closePath();
    return path;
}

var ops = {
    // Page boxes in MuPDF's page space (rotation applied).
    info: function () {
        var pages = [];
        for (var i = 0, n = doc.countPages(); i < n; i++)
            pages.push(doc.loadPage(i).getBounds());
        return {pages: pages};
    },

    // Draw pixel rows [top, bottom) of a page scaled by (sx, sy) into a PNG,
    // optionally with inverted lightness and translucent search marks on top.
    render: function (r) {
        var ctm = [r.sx, 0, 0, r.sy, -r.x0 * r.sx, -r.y0 * r.sy];
        var pix = new Pixmap(ColorSpace.DeviceRGB, [0, r.top, r.width, r.bottom], false);
        pix.clear(255);
        var dev = new DrawDevice(Matrix.identity, pix);
        displayList(r.page).run(dev, ctm);
        dev.close();
        if (r.invert)
            pix.invertLuminance();
        if (r.marks && r.marks.length) {
            dev = new DrawDevice(Matrix.identity, pix);
            r.marks.forEach(function (m) {
                dev.fillPath(quadPath(m.quad), false, ctm, ColorSpace.DeviceRGB, m.color, m.alpha);
            });
            dev.close();
        }
        pix.saveAsPNG(r.out);
        return {width: pix.getWidth(), height: pix.getHeight()};
    },

    // Matches on one page (case-insensitive); each is a list of quads plus its line of text.
    search: function (r) {
        var page = doc.loadPage(r.page), box = page.getBounds();
        var text = page.toStructuredText("preserve-whitespace");
        var hits = text.search(r.needle, r.max || 500).map(function (quads) {
            var y = (quads[0][1] + quads[0][5]) / 2;
            return {quads: quads, text: text.copy([box[0], y], [box[2], y])};
        });
        var empty = hits.length === 0 && text.copy([box[0], box[1]], [box[2], box[3]]).trim() === "";
        return {hits: hits, empty: empty};
    },

    // Quads covering the text from point a to point b, in reading order.
    select: function (r) {
        var t = pageText(r.page), a = endPoint(t, r.a), b = endPoint(t, r.b);
        return {quads: a && b ? t.text.highlight(a, b, 10000) : []};
    },

    // The text from point a to point b.
    copy: function (r) {
        var t = pageText(r.page), a = endPoint(t, r.a), b = endPoint(t, r.b);
        return {text: a && b ? t.text.copy(a, b) : ""};
    },

    // End points that select the word (or line) at point p: a word runs between spaces,
    // without punctuation around it, so double-clicking an identifier in a sentence
    // picks just the identifier.
    snap: function (r) {
        var t = pageText(r.page), best = null;
        t.lines.forEach(function (line, li) {
            line.forEach(function (ch, ci) {
                var d = quadDistance(r.p, ch.quad);
                if (!best || d < best.d)
                    best = {d: d, li: li, ci: ci};
            });
        });
        if (!best || best.d > (r.slop || 6))
            return {};
        var line = t.lines[best.li], s = best.ci, e = best.ci;
        if (r.unit === "line") {
            s = 0;
            e = line.length - 1;
        } else if (!/\s/.test(line[s].c)) {
            while (s > 0 && !/\s/.test(line[s - 1].c))
                s--;
            while (e + 1 < line.length && !/\s/.test(line[e + 1].c))
                e++;
            while (s < best.ci && OPENING.indexOf(line[s].c) >= 0)
                s++;
            while (e > best.ci && CLOSING.indexOf(line[e].c) >= 0)
                e--;
        }
        return {a: inQuad(line[s].quad, 0.25, 0.5), b: inQuad(line[e].quad, 0.75, 0.5)};
    },

    // Links on a page: their rectangles, and where internal ones lead.
    links: function (r) {
        return {links: doc.loadPage(r.page).getLinks().map(function (link) {
            var uri = link.getURI(), external = link.isExternal(), dest = null;
            if (!external) {
                try {
                    dest = doc.resolveLinkDestination(uri);
                } catch (e) {
                    dest = null;
                }
            }
            return {rect: link.getBounds(), uri: uri, external: external,
                    page: dest ? dest.page : -1,
                    y: dest && typeof dest.y === "number" && isFinite(dest.y) ? dest.y : null};
        })};
    },

    // Outline (bookmarks), flattened, with destinations resolved to page and y.
    outline: function () {
        var items = [];
        function walk(nodes, depth) {
            nodes.forEach(function (node) {
                var dest = null;
                try {
                    dest = node.uri ? doc.resolveLinkDestination(node.uri) : null;
                } catch (e) {
                    dest = null;
                }
                items.push({title: node.title || "", depth: depth,
                            page: dest ? dest.page : -1,
                            y: dest && isFinite(dest.y) ? dest.y : null});
                if (node.down)
                    walk(node.down, depth + 1);
            });
        }
        walk(doc.loadOutline() || [], 0);
        return {items: items};
    },
};

// mutool's readline() returns at most 255 bytes, so a request arrives as "+chunk"
// lines followed by a "." line.
function readRequest() {
    var text = "";
    for (;;) {
        var line = readline();
        if (line === null || line === undefined)
            throw "end of input";
        if (line === ".")
            return text;
        text += line.slice(1);
    }
}

for (;;) {
    var text;
    try {
        text = readRequest();
    } catch (e) {
        break;   // stdin closed: the editor went away
    }
    var reply;
    try {
        if (openError)
            throw openError;
        var request = JSON.parse(text);
        reply = ops[request.op](request);
    } catch (e) {
        reply = {error: String(e)};
    }
    print(JSON.stringify(reply));
}
