"""PDF text and annotations via PyMuPDF.

The PDF file is the shared state: PDF viewers save their annotations as
standard PDF annots, and everything here reads and writes those same annots, so
the viewer and the model interoperate through the file with no private format.
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pymupdf

DEFAULT_AUTHOR = os.environ.get("PDF_READER_WRITER_AUTHOR", "llm")
LOCK_TIMEOUT = 10.0  # seconds a reader or writer waits for another writer's flock
# Light blue, so machine highlights are told apart from the usual yellow ones.
DEFAULT_COLOR = (0.6, 0.8, 1.0)
NOTE_COLOR = (0.2, 0.45, 1.0)

# Named colours a caller may pick to keep marks consistent by usage
# (e.g. one colour for definitions, another for open questions).
COLORS = {
    "blue": DEFAULT_COLOR,
    "yellow": (1.0, 0.9, 0.3),
    "green": (0.6, 0.9, 0.6),
    "orange": (1.0, 0.7, 0.4),
    "pink": (1.0, 0.7, 0.85),
    "purple": (0.8, 0.7, 1.0),
    "red": (0.9, 0.3, 0.3),
    "cyan": (0.55, 0.95, 0.95),
    "grey": (0.75, 0.75, 0.75),
}
COLORS["gray"] = COLORS["grey"]


def parse_color(spec: str | None, default: tuple[float, float, float]) -> tuple[float, float, float]:
    """A palette name, ``#rrggbb`` or ``"r,g,b"`` with components in 0-1; ``None`` gives ``default``."""
    if not spec:
        return default
    key = spec.strip().lower()
    if key in COLORS:
        return COLORS[key]
    hexval = key.lstrip("#")
    if len(hexval) == 6 and all(c in "0123456789abcdef" for c in hexval):
        return tuple(int(hexval[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
    try:
        rgb = tuple(float(x) for x in key.split(","))
    except ValueError:
        rgb = ()
    if len(rgb) == 3 and all(0.0 <= c <= 1.0 for c in rgb):
        return rgb  # type: ignore[return-value]
    names = ", ".join(k for k in COLORS if k != "gray")
    raise ValueError(f"unknown colour {spec!r}: use one of {names}, #rrggbb, or 'r,g,b' in 0-1")


_MARKUP = {
    pymupdf.PDF_ANNOT_HIGHLIGHT,
    pymupdf.PDF_ANNOT_UNDERLINE,
    pymupdf.PDF_ANNOT_SQUIGGLY,
    pymupdf.PDF_ANNOT_STRIKE_OUT,
}


def _lock(f: Any, mode: int) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(f, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("PDF is locked by another writer") from None
            time.sleep(0.02)


@contextmanager
def _open(path: str, write: bool = False) -> Iterator[pymupdf.Document]:
    """The document, opened fresh under an flock on the file itself.

    Readers share the lock; a writer holds it exclusively and the document is
    saved incrementally when the block exits without an exception and changed
    something. Nothing is kept open across calls, so a write never works on
    stale bytes and writers in other processes (the edit service, another MCP
    server) are never lost.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    with open(path, "rb") as f:
        _lock(f, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        doc = pymupdf.open(path)
        try:
            yield doc
            if write and doc.is_dirty:
                if not doc.can_save_incrementally():
                    raise RuntimeError("document cannot be saved incrementally (encrypted or repaired?)")
                doc.saveIncr()
        finally:
            doc.close()


def _check_page(doc: pymupdf.Document, page: int) -> None:
    if not 1 <= page <= doc.page_count:
        raise ValueError(f"page {page} outside 1-{doc.page_count}")


def _lines(pg: pymupdf.Page) -> list[dict[str, Any]]:
    """The page's text lines in reading order, numbered from 1, with their block.

    Lines come from PyMuPDF's block/line structure (grouped by position), so a
    two-column page yields the left column first, then the right.
    """
    out: list[dict[str, Any]] = []
    for b, block in enumerate(pg.get_text("dict")["blocks"]):
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                out.append({"n": len(out) + 1, "text": text, "block": b,
                            "bbox": pymupdf.Rect(line["bbox"])})
    return out


def _parse_range(spec: str | None, count: int) -> tuple[int, int]:
    """``"5"`` is line 5 only, ``"5-"`` line 5 to the end, ``"-5"`` up to line 5,
    ``"5-9"`` a range; no spec is the whole page."""
    if not spec:
        return 1, count
    a, dash, b = spec.partition("-")
    try:
        lo = int(a) if a.strip() else 1
        hi = (int(b) if b.strip() else count) if dash else lo
    except ValueError as e:
        raise ValueError(f"lines must look like '12', '12-30', '12-' or '-30', not {spec!r}") from e
    if lo < 1 or hi < lo:
        raise ValueError(f"bad line range {spec!r}")
    return lo, min(hi, count)


def page_text(path: str, page: int, to: int | None = None, lines: str | None = None) -> str:
    """Numbered text lines of pages ``page``..``to`` (1-based, inclusive).

    ``lines`` narrows to a window such as ``"12-30"`` (per page). Numbers are
    per page and match what ``context`` reports.
    """
    with _open(path) as doc:
        last = page if to is None else to
        _check_page(doc, page)
        _check_page(doc, last)
        if last < page:
            raise ValueError(f"to={last} is before page {page}")
        parts = []
        for n in range(page, last + 1):
            ls = _lines(doc[n - 1])
            lo, hi = _parse_range(lines, len(ls))
            body = "\n".join(f"{l['n']}: {l['text']}" for l in ls[lo - 1:hi])
            parts.append(f"--- page {n} ({len(ls)} lines) ---\n{body}")
        return "\n".join(parts)


def _hit_lines(pg: pymupdf.Page, text: str, page: int) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Locate ``text`` on ``pg``: (normalised needle, all page lines, the lines it covers)."""
    needle = " ".join(text.split())
    if not needle:
        raise ValueError("text is empty: pass the user's selection or a quote from the page")
    hits = pg.search_for(needle)
    if not hits:
        # Long selections can cross columns or pages: fall back to their first words.
        hits = pg.search_for(" ".join(needle.split()[:6]))
    if not hits:
        raise LookupError(f"selection not found on page {page}: {needle[:60]!r}")
    ls = _lines(pg)
    # the line that holds a hit's centre: line boxes may touch or overlap
    hit_lines = [l for l in ls if any((h.tl + h.br) / 2 in l["bbox"] for h in hits)]
    if not hit_lines:
        raise LookupError("selection found but not attributable to a text line")
    return needle, ls, hit_lines


def locate(path: str, page: int, text: str) -> dict[str, Any] | None:
    """Where ``text`` sits on ``page``: ``{"page", "lines"}``, or ``None`` if absent."""
    with _open(path) as doc:
        _check_page(doc, page)
        try:
            _, _, hit_lines = _hit_lines(doc[page - 1], text, page)
        except (LookupError, ValueError):
            return None
        return {"page": page, "lines": f"{hit_lines[0]['n']}-{hit_lines[-1]['n']}"}


def context(path: str, page: int, text: str, neighbours: int = 1) -> dict[str, Any]:
    """The paragraph containing ``text`` on ``page``, with ``neighbours`` blocks
    before and after, and the line numbers involved."""
    with _open(path) as doc:
        _check_page(doc, page)
        pg = doc[page - 1]
        needle, ls, hit_lines = _hit_lines(pg, text, page)
        blocks = sorted({l["block"] for l in hit_lines})
        lo_b, hi_b = blocks[0] - neighbours, blocks[-1] + neighbours

        def para(b0: int, b1: int) -> str:
            return "\n".join(l["text"] for l in ls if b0 <= l["block"] <= b1)

        first = min(l["n"] for l in ls if blocks[0] <= l["block"] <= blocks[-1])
        last = max(l["n"] for l in ls if blocks[0] <= l["block"] <= blocks[-1])
        return {
            "page": page,
            "selection": needle,
            "lines": f"{hit_lines[0]['n']}-{hit_lines[-1]['n']}",
            "paragraph_lines": f"{first}-{last}",
            "before": para(lo_b, blocks[0] - 1) if neighbours else "",
            "paragraph": para(blocks[0], blocks[-1]),
            "after": para(blocks[-1] + 1, hi_b) if neighbours else "",
        }


def outline(path: str) -> list[dict[str, Any]]:
    """The document's bookmark tree (table of contents): level, title, page."""
    with _open(path) as doc:
        return [{"level": lvl, "title": title, "page": page}
                for lvl, title, page in doc.get_toc(simple=True)]


def _line_at(ls: list[dict[str, Any]], y: float) -> int | None:
    """Number of the first line starting at or below ``y`` (top-left coordinates);
    link anchors sit just above their target line."""
    for l in ls:
        if l["bbox"].y0 >= y - 2:
            return l["n"]
    return ls[-1]["n"] if ls else None


def links(path: str, page: int) -> list[dict[str, Any]]:
    """Links on ``page``: anchor text and line, then either the target page and
    line (internal links, e.g. citations, sections, figures) or the URL."""
    out: list[dict[str, Any]] = []
    with _open(path) as doc:
        _check_page(doc, page)
        pg = doc[page - 1]
        ls = _lines(pg)
        target_lines: dict[int, list[dict[str, Any]]] = {}
        for link in pg.get_links():
            r = pymupdf.Rect(link["from"])
            r.y0 += r.height * 0.2
            r.y1 -= r.height * 0.2
            entry: dict[str, Any] = {
                "text": " ".join(pg.get_textbox(r).split()),
                "line": next((l["n"] for l in ls if l["bbox"].intersects(r)), None),
            }
            kind = link["kind"]
            if kind == pymupdf.LINK_URI:
                entry["url"] = link.get("uri", "")
            elif kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and link.get("page", -1) >= 0:
                tp = link["page"] + 1
                entry["page"] = tp
                if link.get("nameddest"):
                    entry["dest"] = link["nameddest"]
                to = link.get("to")
                if to is not None and 1 <= tp <= doc.page_count:
                    target = doc[tp - 1]
                    y = pymupdf.Point(to).y
                    if kind == pymupdf.LINK_NAMED:
                        # Resolved named destinations keep PDF (bottom-up) coordinates,
                        # unlike plain GoTo links, which PyMuPDF already converts.
                        y = target.rect.height - y
                    if tp not in target_lines:
                        target_lines[tp] = _lines(target)
                    entry["target_line"] = _line_at(target_lines[tp], y)
            else:
                continue  # launch/unknown links are not useful to a reader
            out.append(entry)
    return out


def _marked_text(pg: pymupdf.Page, annot: pymupdf.Annot) -> str:
    """Words of the page whose centre line lies inside one of the annot's quads.

    Quads follow the layout, so a highlight over a line break is several quads
    and one word can be split across two; collecting whole words by index avoids
    partial or duplicated words.
    """
    verts = annot.vertices or []
    quads = [pymupdf.Quad(verts[i:i + 4]).rect for i in range(0, len(verts) - 3, 4)] or [annot.rect]
    words = pg.get_text("words")  # (x0, y0, x1, y1, word, block, line, wordno)
    picked: list[int] = []
    for q in quads:
        for i, w in enumerate(words):
            r = pymupdf.Rect(w[:4])
            ymid = (r.y0 + r.y1) / 2
            if q.y0 <= ymid <= q.y1 and min(r.x1, q.x1) - max(r.x0, q.x0) > 0.3 * r.width:
                if i not in picked:
                    picked.append(i)
    return " ".join(words[i][4] for i in sorted(picked))


def to_pdf_quad(pg: pymupdf.Page, q: pymupdf.Quad) -> list[float]:
    """A quad in PDF user space (origin bottom-left), as 8 numbers in /QuadPoints
    order: upper-left, upper-right, lower-left, lower-right."""
    m = ~pg.transformation_matrix
    return [round(c, 3) for p in (q.ul, q.ur, q.ll, q.lr) for c in tuple(p * m)]


def to_pdf_rect(pg: pymupdf.Page, r: pymupdf.Rect) -> list[float]:
    """A rect in PDF user space, ``[x0, y0, x1, y1]`` with ``x0 < x1`` and ``y0 < y1``."""
    return [round(c, 3) for c in tuple((r * ~pg.transformation_matrix).normalize())]


def from_pdf_rect(pg: pymupdf.Page, r: list[float]) -> pymupdf.Rect:
    """A PDF user-space rect in PyMuPDF's (unrotated, top-left origin) page space."""
    return (pymupdf.Rect(r) * pg.transformation_matrix).normalize()


def from_pdf_point(pg: pymupdf.Page, p: list[float]) -> pymupdf.Point:
    return pymupdf.Point(p) * pg.transformation_matrix


def from_pdf_quad(pg: pymupdf.Page, q: list[float]) -> pymupdf.Quad:
    m = pg.transformation_matrix
    pts = [pymupdf.Point(q[i], q[i + 1]) * m for i in range(0, 8, 2)]
    return pymupdf.Quad(*pts)


def annot_quads(annot: pymupdf.Annot) -> list[pymupdf.Quad]:
    verts = annot.vertices or []
    return [pymupdf.Quad(verts[i:i + 4]) for i in range(0, len(verts) - 3, 4)]


def _entry(pg: pymupdf.Page, n: int, annot: pymupdf.Annot, geometry: bool) -> dict[str, Any]:
    kind, name = annot.type[0], annot.type[1]
    info = annot.info
    entry: dict[str, Any] = {
        "page": n,
        "type": name,
        "author": info.get("title", ""),
        "note": info.get("content", ""),
        "modified": info.get("modDate", ""),
        "xref": annot.xref,
    }
    if kind in _MARKUP:
        entry["text"] = _marked_text(pg, annot)
    if kind == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
        fi = annot.file_info
        entry["file"] = fi.get("filename", "")
        entry["bytes"] = fi.get("size", 0)
    if annot.irt_xref:
        entry["reply_to"] = annot.irt_xref
    if annot.colors.get("stroke"):
        entry["color"] = [round(c, 2) for c in annot.colors["stroke"]]
    if geometry:
        entry["irt"] = annot.irt_xref or 0
        entry["quads"] = [to_pdf_quad(pg, q) for q in annot_quads(annot)] if kind in _MARKUP else []
        entry["rect"] = to_pdf_rect(pg, annot.rect)
    return entry


def annotations(path: str, page: int | None = None, geometry: bool = False) -> list[dict[str, Any]]:
    """Every annotation, with the text under markup annots and the note contents.

    ``geometry`` adds ``irt`` (0 when none), ``quads`` and ``rect`` in PDF user space."""
    out: list[dict[str, Any]] = []
    with _open(path) as doc:
        if page is not None:
            _check_page(doc, page)
        pages = [page] if page is not None else range(1, doc.page_count + 1)
        for n in pages:
            pg = doc[n - 1]
            for annot in pg.annots():
                out.append(_entry(pg, n, annot, geometry))
    return out


_STYLES = {
    "highlight": ("add_highlight_annot", DEFAULT_COLOR),
    "underline": ("add_underline_annot", NOTE_COLOR),
    "squiggly": ("add_squiggly_annot", NOTE_COLOR),
    "strikeout": ("add_strikeout_annot", (0.9, 0.3, 0.3)),
}


def set_contents(annot: pymupdf.Annot, text: str) -> None:
    """Set the note (/Contents), the empty string included: Annot.set_info skips
    empty content, so a note could never be cleared through it. A Popup that
    carries its own copy of /Contents is kept in step."""
    pymupdf.mupdf.pdf_set_annot_contents(annot.this, text)
    if annot.has_popup:
        doc = annot.get_parent().parent
        if doc.xref_get_key(annot.popup_xref, "Contents")[0] != "null":
            doc.xref_set_key(annot.popup_xref, "Contents", pymupdf.get_pdf_str(text))


def style_of(subtype: str) -> str | None:
    """The style name for a markup /Subtype (``StrikeOut`` -> ``strikeout``), else None."""
    key = subtype.lower()
    return key if key in _STYLES else None


def add_markup(pg: pymupdf.Page, quads: list[pymupdf.Quad], style: str,
               rgb: tuple[float, float, float], note: str, author: str,
               created: str | None = None, modified: str | None = None,
               opacity: float | None = None) -> pymupdf.Annot:
    """One markup annotation over ``quads``, with author, note, colour and dates."""
    if style not in _STYLES:
        raise ValueError(f"style must be one of {', '.join(_STYLES)}")
    annot = getattr(pg, _STYLES[style][0])(quads)
    annot.set_colors(stroke=rgb)
    if opacity is not None:
        annot.set_opacity(opacity)
    now = pymupdf.get_pdf_now()
    annot.set_info(title=author, creationDate=created or now, modDate=modified or now)
    set_contents(annot, note)
    annot.update()
    return annot


def _find_quads(pg: pymupdf.Page, text: str, page: int) -> list[pymupdf.Quad]:
    quads = pg.search_for(text, quads=True)
    if not quads:
        # Retry with whitespace normalised: selections often carry line breaks.
        quads = pg.search_for(" ".join(text.split()), quads=True)
    if not quads:
        raise LookupError(f"text not found on page {page}: {text[:60]!r}")
    return quads


def _margin_point(pg: pymupdf.Page, y: float) -> pymupdf.Point:
    """A spot in the left margin level with ``y``, clear of the text area."""
    blocks = pg.get_text("blocks")
    left = min((b[0] for b in blocks), default=pg.rect.x0 + 40)
    x = max(pg.rect.x0 + 2, left - 24)
    return pymupdf.Point(x, max(pg.rect.y0 + 2, y - 2))


def note_annot(pg: pymupdf.Page, point: pymupdf.Point, note: str, author: str,
               reply_to: int | None = None,
               color: tuple[float, float, float] = NOTE_COLOR, icon: str = "Comment",
               created: str | None = None, modified: str | None = None) -> pymupdf.Annot:
    n = pg.add_text_annot(point, note, icon=icon)
    now = pymupdf.get_pdf_now()
    n.set_info(title=author, creationDate=created or now, modDate=modified or now)
    set_contents(n, note)
    n.set_colors(stroke=color)
    if reply_to:
        n.set_irt_xref(reply_to)
    n.update()
    return n


def mark_text(path: str, page: int, text: str, note: str | None = None,
              style: str = "highlight", note_in_margin: bool = False,
              color: str | None = None, author: str = DEFAULT_AUTHOR) -> dict[str, Any]:
    """Mark every occurrence of ``text`` on ``page`` and save incrementally.

    ``style`` is highlight, underline, squiggly or strikeout. The note goes on the
    mark itself (shown when hovered/opened) or, with ``note_in_margin``, on a
    separate comment icon in the margin that replies to the mark, which keeps
    the text itself quiet. ``color`` overrides the style's default colour: a name
    from ``COLORS``, ``#rrggbb`` or an ``"r,g,b"`` triple in 0-1.
    """
    if style not in _STYLES:
        raise ValueError(f"style must be one of {', '.join(_STYLES)}")
    rgb = parse_color(color, _STYLES[style][1])
    with _open(path, write=True) as doc:
        _check_page(doc, page)
        pg = doc[page - 1]
        quads = _find_quads(pg, text, page)
        on_mark = "" if note_in_margin else (note or "")
        annot = add_markup(pg, quads, style, rgb, on_mark, author)
        result = {"page": page, "xref": annot.xref, "style": style, "text": text,
                  "note": note or "", "author": author,
                  "color": [round(c, 2) for c in rgb]}
        if note and note_in_margin:
            n = note_annot(pg, _margin_point(pg, quads[0].rect.y0), note, author, annot.xref,
                           parse_color(color, NOTE_COLOR))
            result["note_xref"] = n.xref
    return result


def add_highlight(path: str, page: int, text: str, note: str | None = None,
                  author: str = DEFAULT_AUTHOR) -> dict[str, Any]:
    return mark_text(path, page, text, note, author=author)


def add_note(path: str, page: int | None, note: str, near_text: str | None = None,
             reply_to: int | None = None, color: str | None = None,
             author: str = DEFAULT_AUTHOR) -> dict[str, Any]:
    """A comment icon in the margin: next to ``near_text``, as a threaded reply to
    annotation ``reply_to`` (page found automatically), or at the top of the page."""
    with _open(path, write=True) as doc:
        if reply_to:
            pg, target, page = find_annot(doc, reply_to)
            point = _margin_point(pg, target.rect.y0)
        else:
            if page is None:
                raise ValueError("page is required unless reply_to is given")
            _check_page(doc, page)
            pg = doc[page - 1]
            if near_text:
                point = _margin_point(pg, _find_quads(pg, near_text, page)[0].rect.y0)
            else:
                blocks = pg.get_text("blocks")
                point = _margin_point(pg, min((b[1] for b in blocks), default=pg.rect.y0 + 40))
        n = note_annot(pg, point, note, author, reply_to, parse_color(color, NOTE_COLOR))
        result = {"page": page, "xref": n.xref, "note": note, "author": author,
                  "reply_to": reply_to or 0}
    return result


def find_annot(doc: pymupdf.Document, xref: int) -> tuple[pymupdf.Page, pymupdf.Annot, int]:
    """Locate an annotation by xref; the Page is returned so it outlives the Annot
    (an Annot that outlives its Page crashes PyMuPDF)."""
    for n in range(1, doc.page_count + 1):
        pg = doc[n - 1]
        for annot in pg.annots():
            if annot.xref == xref:
                return pg, annot, n
    raise LookupError(f"no annotation with xref {xref}")


def thread(doc: pymupdf.Document, xref: int) -> list[int]:
    """``xref`` and every reply to it, recursively, parents before their replies."""
    children: dict[int, list[int]] = {}
    for pg in doc:
        for annot in pg.annots():
            if annot.irt_xref:
                children.setdefault(annot.irt_xref, []).append(annot.xref)
    out: list[int] = []
    todo = [xref]
    while todo:
        x = todo.pop(0)
        if x not in out:
            out.append(x)
            todo[:0] = children.get(x, [])
    return out


def delete_thread(doc: pymupdf.Document, xref: int) -> list[int]:
    """Delete ``xref`` and all its replies, replies first.

    PyMuPDF's delete_annot drops an annot's direct replies but leaves replies to
    those replies pointing at nothing, so the whole thread is walked here. The
    pdfnotes ids among them are recorded in the catalog for ``source.sync``."""
    from . import source  # source imports this module, so not at the top

    xrefs = thread(doc, xref)
    ids = [i for i in (source.nm_id(doc, x) for x in xrefs) if i]
    for x in reversed(xrefs):
        try:
            pg, annot, _ = find_annot(doc, x)
        except LookupError:
            continue  # already taken by delete_annot together with its parent
        pg.delete_annot(annot)
    source.record_deleted(doc, ids)  # so that sync_to_source removes their macros
    return xrefs


def remove_annotation(path: str, xref: int, page: int | None = None) -> dict[str, Any]:
    """Delete the annotation with PDF object number ``xref``, with its replies
    (recursively), and save incrementally."""
    with _open(path, write=True) as doc:
        if page is not None:
            _check_page(doc, page)
        pg, annot, n = find_annot(doc, xref)
        if page is not None and n != page:
            raise LookupError(f"no annotation with xref {xref} on page {page}")
        info = {"page": n, "xref": xref, "type": annot.type[1],
                "author": annot.info.get("title", "")}
        info["replies"] = delete_thread(doc, xref)[1:]
    return info


def attach_file(path: str, page: int | None, name: str, content: str,
                note: str | None = None, near_text: str | None = None,
                replace_xref: int | None = None, color: str | None = None,
                author: str = DEFAULT_AUTHOR) -> dict[str, Any]:
    """Embed ``content`` as a file attachment annotation (paperclip in the margin),
    or replace the file of an existing attachment ``replace_xref``."""
    data = content.encode("utf-8")
    with _open(path, write=True) as doc:
        now = pymupdf.get_pdf_now()
        if replace_xref:
            pg, annot, page = find_annot(doc, replace_xref)
            if annot.type[0] != pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
                raise ValueError(f"xref {replace_xref} is a {annot.type[1]}, not a file attachment")
            annot.update_file(data, filename=name, ufilename=name, desc=note or "")
            set_contents(annot, note or "")
            annot.set_info(modDate=now)
            annot.update()
        else:
            if page is None:
                raise ValueError("page is required unless replace_xref is given")
            _check_page(doc, page)
            pg = doc[page - 1]
            if near_text:
                y = _find_quads(pg, near_text, page)[0].rect.y0
            else:
                blocks = pg.get_text("blocks")
                y = min((b[1] for b in blocks), default=pg.rect.y0 + 40)
            annot = pg.add_file_annot(_margin_point(pg, y), data, name, ufilename=name,
                                      desc=note or "", icon="Paperclip")
            annot.set_info(title=author, creationDate=now, modDate=now)
            set_contents(annot, note or "")
            annot.set_colors(stroke=parse_color(color, NOTE_COLOR))
            annot.update()
        result = {"page": page, "xref": annot.xref, "name": name, "bytes": len(data),
                  "note": note or ""}
    return result


def read_attachment(path: str, xref: int) -> dict[str, Any]:
    """The file of a file-attachment annotation, decoded as UTF-8 text."""
    with _open(path) as doc:
        pg, annot, page = find_annot(doc, xref)
        if annot.type[0] != pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
            raise ValueError(f"xref {xref} is a {annot.type[1]}, not a file attachment")
        info = annot.file_info
        data = annot.get_file()
        return {"page": page, "xref": xref, "name": info.get("filename", ""),
                "note": annot.info.get("content", ""), "author": annot.info.get("title", ""),
                "bytes": len(data), "content": data.decode("utf-8", errors="replace")}
