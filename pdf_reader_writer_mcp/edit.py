"""The edit service: PDF marking for a viewer, as JSON lines over stdin/stdout.

A viewer (the VS Code PDF viewer) spawns one long-lived ``pdf-reader-writer-edit``
process and sends it the user's marks, so the user and the model write through
the same PyMuPDF code and the same file lock. EDITING.md is the protocol.
"""

from __future__ import annotations

import base64
import inspect
import json
import sys
from typing import Any

import pymupdf

from . import pdf

VERSION = 1


class RequestError(ValueError):
    """A malformed request: missing, unknown or mistyped field, unknown op."""


def _check_author(annot: pymupdf.Annot, only_author: str | None) -> None:
    if only_author is not None and annot.info.get("title", "") != only_author:
        raise PermissionError("not your annotation")


def _snap(pg: pymupdf.Page, boxes: list[pymupdf.Rect]) -> tuple[list[pymupdf.Quad], str]:
    """The words whose centre lies in one of ``boxes``, merged into one quad per
    text line, and their text in reading order."""
    lines: dict[tuple[int, int], list[Any]] = {}
    for w in pg.get_text("words"):  # (x0, y0, x1, y1, word, block, line, wordno)
        r = pymupdf.Rect(w[:4])
        centre = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        if any(centre in b for b in boxes):
            lines.setdefault((w[5], w[6]), []).append(w)
    quads, words = [], []
    for ws in lines.values():
        r = pymupdf.Rect(ws[0][:4])
        for w in ws[1:]:
            r |= pymupdf.Rect(w[:4])
        quads.append(r.quad)
        words += [w[4] for w in ws]
    return quads, " ".join(words)


def mark(path: str, page: int, rects: list, author: str, text: str | None = None,
         style: str = "highlight", color: str | None = None, note: str | None = None) -> dict[str, Any]:
    """One markup annotation over the words under ``rects`` (PDF user space)."""
    if style not in pdf._STYLES:
        raise ValueError(f"style must be one of {', '.join(pdf._STYLES)}")
    if not rects:
        raise RequestError("rects is empty")
    rgb = pdf.parse_color(color, pdf._STYLES[style][1])
    with pdf._open(path, write=True) as doc:
        pdf._check_page(doc, page)
        pg = doc[page - 1]
        boxes = [pdf.from_pdf_rect(pg, r) for r in rects]
        quads, snapped = _snap(pg, boxes)
        if not quads:  # no text under the selection (a figure, a scanned page): mark the rects
            quads, snapped = [b.quad for b in boxes], text or ""
        annot = pdf.add_markup(pg, quads, style, rgb, note or "", author)
        result = {"xref": annot.xref, "page": page,
                  "quads": [pdf.to_pdf_quad(pg, q) for q in quads], "text": snapped}
    return result


def note(path: str, page: int, point: list, note: str, author: str,
         color: str | None = None) -> dict[str, Any]:
    """A comment icon (Text annotation) at ``point`` (PDF user space)."""
    with pdf._open(path, write=True) as doc:
        pdf._check_page(doc, page)
        pg = doc[page - 1]
        n = pdf.note_annot(pg, pdf.from_pdf_point(pg, point), note, author,
                           color=pdf.parse_color(color, pdf.NOTE_COLOR))
        result = {"xref": n.xref, "page": page}
    return result


def reply(path: str, xref: int, note: str, author: str) -> dict[str, Any]:
    """A reply in the thread of annotation ``xref``, as add_note(reply_to=) makes it."""
    r = pdf.add_note(path, None, note, reply_to=xref, author=author)
    return {"xref": r["xref"], "page": r["page"]}


def update(path: str, xref: int, note: str | None = None, color: str | None = None,
           style: str | None = None, only_author: str | None = None) -> dict[str, Any]:
    """Set the note and/or colour; a new markup style recreates the annotation."""
    if style is not None and style not in pdf._STYLES:
        raise ValueError(f"style must be one of {', '.join(pdf._STYLES)}")
    with pdf._open(path, write=True) as doc:
        pg, annot, _ = pdf.find_annot(doc, xref)
        _check_author(annot, only_author)
        subtype = annot.type[1]
        current = pdf.style_of(subtype)
        if style is not None and current is None:
            raise ValueError(f"xref {xref} is a {subtype}, not a text markup")
        now = pymupdf.get_pdf_now()
        if style is not None and style != current:
            info = annot.info
            stroke = annot.colors.get("stroke")
            rgb = pdf.parse_color(color, tuple(stroke) if stroke else pdf._STYLES[style][1])
            new = pdf.add_markup(pg, pdf.annot_quads(annot) or [annot.rect.quad], style, rgb,
                                 info.get("content", "") if note is None else note,
                                 info.get("title", ""), created=info.get("creationDate") or None,
                                 modified=now, opacity=annot.opacity if 0 <= annot.opacity < 1 else None)
            new_xref = new.xref
            # Re-point the replies first: delete_annot would take them with the old annot.
            for p in doc:
                for a in p.annots():
                    if a.irt_xref == xref:
                        doc.xref_set_key(a.xref, "IRT", f"{new_xref} 0 R")
            pg, old, _ = pdf.find_annot(doc, xref)
            pg.delete_annot(old)
            return {"xref": new_xref}
        if note is not None:
            annot.set_info(content=note)
        if color is not None:
            annot.set_colors(stroke=pdf.parse_color(color, pdf.DEFAULT_COLOR))
        annot.set_info(modDate=now)
        if color is not None:
            annot.update()
    return {"xref": xref}


def _snapshot(doc: pymupdf.Document, xref: int) -> dict[str, Any]:
    pg, a, n = pdf.find_annot(doc, xref)
    info = a.info
    kind, subtype = a.type[0], a.type[1]
    icon = doc.xref_get_key(xref, "Name")
    stroke = a.colors.get("stroke")
    snap: dict[str, Any] = {
        "xref": xref,
        "page": n,
        "subtype": subtype,
        "rect": pdf.to_pdf_rect(pg, a.rect),
        "quads": [pdf.to_pdf_quad(pg, q) for q in pdf.annot_quads(a)] if kind in pdf._MARKUP else [],
        "color": [round(c, 4) for c in stroke] if stroke else None,
        "opacity": a.opacity if 0 <= a.opacity < 1 else None,
        "contents": info.get("content", ""),
        "author": info.get("title", ""),
        "irt": a.irt_xref or 0,
        "icon": icon[1].lstrip("/") if icon[0] == "name" else None,
        "created": info.get("creationDate", ""),
        "modified": info.get("modDate", ""),
    }
    if kind == pymupdf.PDF_ANNOT_FILE_ATTACHMENT:
        fi = a.file_info
        snap["file"] = {"name": fi.get("filename", ""), "desc": fi.get("description", ""),
                        "data_base64": base64.b64encode(a.get_file()).decode("ascii")}
    return snap


def snapshot(path: str, xref: int) -> dict[str, Any]:
    """Everything needed to recreate annotation ``xref``."""
    with pdf._open(path) as doc:
        return _snapshot(doc, xref)


def delete(path: str, xref: int, only_author: str | None = None) -> dict[str, Any]:
    """Delete ``xref``, its Popup and its replies (recursively); the snapshots,
    parent first, undo it through ``restore``."""
    with pdf._open(path, write=True) as doc:
        _, annot, _ = pdf.find_annot(doc, xref)
        _check_author(annot, only_author)
        snaps = [_snapshot(doc, x) for x in pdf.thread(doc, xref)]
        pdf.delete_thread(doc, xref)
    return {"snapshots": snaps}


def _restore_one(doc: pymupdf.Document, s: dict[str, Any], irt: int) -> pymupdf.Annot:
    pdf._check_page(doc, s["page"])
    pg = doc[s["page"] - 1]
    subtype = s["subtype"]
    color = tuple(s["color"]) if s.get("color") else None
    created, modified = s.get("created") or None, s.get("modified") or None
    style = pdf.style_of(subtype)
    if style:
        quads = [pdf.from_pdf_quad(pg, q) for q in s.get("quads") or []]
        a = pdf.add_markup(pg, quads or [pdf.from_pdf_rect(pg, s["rect"]).quad], style,
                           color or pdf._STYLES[style][1], s.get("contents", ""), s.get("author", ""),
                           created, modified, s.get("opacity"))
    elif subtype == "Text":
        a = pdf.note_annot(pg, pdf.from_pdf_rect(pg, s["rect"]).top_left, s.get("contents", ""),
                           s.get("author", ""), color=color or pdf.NOTE_COLOR,
                           icon=s.get("icon") or "Note", created=created, modified=modified)
    elif subtype == "FileAttachment" and s.get("file"):
        f = s["file"]
        a = pg.add_file_annot(pdf.from_pdf_rect(pg, s["rect"]).top_left,
                              base64.b64decode(f["data_base64"]), f["name"], ufilename=f["name"],
                              desc=f.get("desc", ""), icon=s.get("icon") or "PushPin")
        now = pymupdf.get_pdf_now()
        a.set_info(title=s.get("author", ""), content=s.get("contents", ""),
                   creationDate=created or now, modDate=modified or now)
        if color:
            a.set_colors(stroke=color)
        a.update()
    else:
        raise ValueError(f"cannot restore a {subtype} annotation")
    if s.get("opacity") is not None and not style:
        a.set_opacity(s["opacity"])
        a.update()
    if irt:
        a.set_irt_xref(irt)
        a.update()
    return a


def restore(path: str, snapshots: list) -> dict[str, Any]:
    """Recreate ``snapshots`` in order, re-pointing replies at the recreated
    parents; all or nothing."""
    mapping: dict[int, int] = {}
    with pdf._open(path, write=True) as doc:
        for s in snapshots:
            if not isinstance(s, dict) or not {"xref", "page", "subtype", "rect"} <= s.keys():
                raise RequestError("each snapshot needs at least xref, page, subtype and rect")
            irt = s.get("irt") or 0
            a = _restore_one(doc, s, mapping.get(irt, irt))
            mapping[s["xref"]] = a.xref
    return {"xrefs": {str(k): v for k, v in mapping.items()}}


def list_(path: str) -> list[dict[str, Any]]:
    """The MCP ``annotations`` records plus ``irt``, ``quads`` and ``rect``."""
    return pdf.annotations(path, geometry=True)


def ping() -> dict[str, Any]:
    return {"version": VERSION}


OPS = {"mark": mark, "note": note, "reply": reply, "update": update, "delete": delete,
       "snapshot": snapshot, "restore": restore, "list": list_, "ping": ping}

_TYPES: dict[str, tuple[type, str]] = {
    "path": (str, "a string"), "page": (int, "an integer"), "xref": (int, "an integer"),
    "rects": (list, "a list"), "point": (list, "a list"), "note": (str, "a string"),
    "author": (str, "a string"), "text": (str, "a string"), "style": (str, "a string"),
    "color": (str, "a string"), "only_author": (str, "a string"), "snapshots": (list, "a list"),
}


def _numbers(v: Any, n: int) -> bool:
    return (isinstance(v, list) and len(v) == n
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v))


def _dispatch(req: dict[str, Any]) -> Any:
    op = req.get("op")
    if op is None:
        raise RequestError("missing field: op")
    fn = OPS.get(op) if isinstance(op, str) else None
    if fn is None:
        raise RequestError(f"unknown op: {op}")
    params = inspect.signature(fn).parameters
    fields = {k: v for k, v in req.items() if k not in ("id", "op") and v is not None}
    for k in req:
        if k not in ("id", "op") and k not in params:
            raise RequestError(f"unknown field: {k}")
    for name, p in params.items():
        if p.default is inspect.Parameter.empty and name not in fields:
            raise RequestError(f"missing field: {name}")
    for k, v in fields.items():
        typ, what = _TYPES[k]
        if not isinstance(v, typ) or isinstance(v, bool):
            raise RequestError(f"field {k} must be {what}")
    if "rects" in fields and not all(_numbers(r, 4) for r in fields["rects"]):
        raise RequestError("rects must be a list of [x0, y0, x1, y1]")
    if "point" in fields and not _numbers(fields["point"], 2):
        raise RequestError("point must be [x, y]")
    return fn(**fields)


def handle(req: Any) -> dict[str, Any]:
    """One request object to one response object; never raises."""
    if not isinstance(req, dict):
        return {"id": None, "ok": False, "error": "bad request: not a JSON object"}
    rid = req.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return {"id": None, "ok": False, "error": "bad request: id must be an integer"}
    try:
        return {"id": rid, "ok": True, "result": _dispatch(req)}
    except (ValueError, LookupError, OSError, RuntimeError) as e:
        return {"id": rid, "ok": False, "error": str(e)}
    except Exception as e:  # a bug: report it and keep serving
        return {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    out = sys.stdout
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol
    pymupdf.set_messages(fd=2)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            resp = {"id": None, "ok": False, "error": f"bad request: {e}"}
        else:
            resp = handle(req)
        try:
            out.write(json.dumps(resp) + "\n")
            out.flush()
        except BrokenPipeError:
            break


if __name__ == "__main__":
    main()
