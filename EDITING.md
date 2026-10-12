# The edit service (`pdf-reader-writer-edit`), protocol v1

A PDF viewer that lets the user mark PDFs (the VS Code viewer of
[vscode-pdf-mcp](https://github.com/david-wang-0/vscode-pdf-mcp)) writes through this
package instead of its own PDF editor. The user's marks and the model's (the MCP
tools) then go through one writing path and one file lock, and come out as standard
annotations that Okular and other viewers read.

## Process

`pdf-reader-writer-edit` (same package, no MCP) is long-lived: the viewer spawns one
per window and talks **JSON lines** over stdin/stdout.

- Request: `{"id": <int>, "op": "<op>", ...fields}`, one line.
- Response: `{"id": <int>, "ok": true, "result": ...}` or
  `{"id": <int>, "ok": false, "error": "<message>"}`, one line.
- Requests are handled in order, one at a time. Blank lines are skipped.
- A line that is not JSON, not an object, or has no integer `id` gets
  `{"id": null, "ok": false, "error": "bad request: <detail>"}`; the process keeps running.
- EOF on stdin: exit 0.
- Only responses go to stdout; MuPDF messages and anything else go to stderr.

From a checkout: `.venv/bin/pdf-reader-writer-edit`, or `python -m pdf_reader_writer_mcp.edit`.

## Locking

Every access takes `fcntl.flock` on the PDF file itself: **shared** for reads,
**exclusive** for writes. That covers these ops *and* the MCP tools. A write opens
the document fresh under the lock, modifies it, saves incrementally (`saveIncr`),
closes it, and releases the lock. No document is kept open across requests, so a
write never works on stale bytes and a concurrent writer is never lost. A
reader never sees a half-written save.

Waiting for the lock gives up after 10 s with `PDF is locked by another writer`.

**Okular does not take the lock.** It writes only when the user saves, and it reloads
files changed on disk. Unsaved Okular annotations are lost if the user accepts that
reload, so agents still ask Okular users to save before writing.

## Geometry

All geometry is in **PDF user space**: origin bottom-left, unrotated page, as pdf.js
`viewport.convertToPdfPoint` reports it. `/Rotate` is handled.

- point: `[x, y]`
- rect: `[x0, y0, x1, y1]` with `x0 < x1`, `y0 < y1`
- quad: 8 numbers `[x1, y1, x2, y2, x3, y3, x4, y4]` in `/QuadPoints` order: upper-left,
  upper-right, lower-left, lower-right. `quads` is a list of those.

## Fields

Types are checked: `path`, `note`, `author`, `text`, `style`, `color`, `only_author`
are strings; `page` and `xref` integers; `rects`, `point`, `snapshots` lists. An
optional field may be `null` (same as absent). Errors:

| Case | `error` |
|---|---|
| required field missing | `missing field: <name>` |
| field the op does not take | `unknown field: <name>` |
| wrong type | `field <name> must be a string` / `an integer` / `a list` |
| unknown op | `unknown op: <op>` |
| file missing | `no such file: <path>` |
| page out of range | `page N outside 1-M` |
| no such annotation | `no annotation with xref N` |
| `only_author` differs from /T | `not your annotation` |
| lock wait timed out | `PDF is locked by another writer` |

## Ops

Fields marked `?` are optional.

**`mark`** `{path, page, rects, author, text?, style?, color?, note?}`
One markup annotation. `style` is `highlight` (default), `underline`, `squiggly` or
`strikeout`. The rects snap to text: the words whose box centre lies in a rect are
taken and merged into one quad per text line. With no word hit (a figure, a scanned
page), the rects are used as given, and `text` is reported as the text. The annotation
gets all quads, `/T` = `author` and `/Contents` = `note`.
→ `{xref, page, quads, text}`, where `text` is the snapped words in reading order.
Errors: `rects is empty`, `rects must be a list of [x0, y0, x1, y1]`,
`style must be one of …`, `unknown colour …`.

**`note`** `{path, page, point, note, author, color?}`
A Text annotation (comment icon) whose top-left corner is at `point`. → `{xref, page}`.
Errors: `point must be [x, y]`.

**`reply`** `{path, xref, note, author}`
A reply in the thread of annotation `xref` (`/IRT`), placed in the margin level with
it, as the MCP `add_note(reply_to=)` does. → `{xref, page}`.

**`update`** `{path, xref, note?, color?, style?, only_author?}`
Sets `/Contents` and/or the colour in place and stamps the modification date.
→ `{xref}`. A `style` different from the current one recreates the annotation with
the same quads, note, author, colour (unless `color` is given) and creation date, and a
new modification date. The direct replies' `/IRT` is re-pointed to the new xref,
and the result is `{xref: <new>}`. `style` on a non-markup annotation fails with
`xref N is a <Subtype>, not a text markup`. With `only_author`, the annotation's `/T`
must equal it.

**`delete`** `{path, xref, only_author?}`
Deletes the annotation, its Popup, and every reply to it, recursively. (PyMuPDF's own
deletion drops only direct replies and orphans replies to replies.)
→ `{snapshots: [...]}` for the whole thread, parent first, ready for `restore`.

**`snapshot`** `{path, xref}`
→ everything needed to recreate the annotation:
`{xref, page, subtype, rect, quads, color, opacity, contents, author, irt, icon,
created, modified}`. For a FileAttachment it also has
`file: {name, desc, data_base64}`. `quads` is `[]` for non-markup, `irt` is `0` for
no thread, and `color`, `opacity` and `icon` can be `null`.

**`restore`** `{path, snapshots}`
Recreates the snapshots in order. An `irt` that names an earlier snapshot in the
list is remapped to its new xref, so a deleted thread comes back as a thread. All or
nothing: on any error, nothing is written. → `{xrefs: {"<old>": <new>, ...}}` (JSON
object keys are strings). Restorable subtypes: Highlight, Underline, Squiggly,
StrikeOut, Text, FileAttachment. Anything else fails with
`cannot restore a <Subtype> annotation`. Errors:
`each snapshot needs at least xref, page, subtype and rect`.

**`list`** `{path}`
→ the records of the MCP `annotations` tool (`page, type, author, note, modified,
xref`, plus `text` for markup, `file`/`bytes` for attachments, `reply_to` and `color`
when set), each with `irt` (0 when none), `quads` (`[]` for non-markup) and `rect`.

**`ping`** `{}` → `{version: 1}`.

## Colours

The same palette and parsing as the MCP tools: `blue`, `yellow`, `green`, `orange`,
`pink`, `purple`, `red`, `cyan`, `grey`, `#rrggbb`, or `r,g,b` in 0–1. The defaults
are light blue for highlights, blue for underline, squiggly and notes, and red for
strikeout.

## Tests

`tests/test_edit.py` pins every op and error message above. It also checks that a
two-line selection snaps to two quads with its text recovered, that a style change
keeps the thread, and that delete + restore round-trips a thread. On concurrency, an
edit-service process and the MCP `mark_text`/`add_note` path each write N annotations
to one file at the same time, then two edit-service processes do the same: all 2N
annotations must be present and the file must open without repair. Without the lock,
that test hangs or loses annotations. Finally, the written objects are checked in
Okular terms (`/Subtype`, `/QuadPoints`, `/T`, `/Contents`, `/IRT`).
