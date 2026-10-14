# pdf-reader-writer-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM session **read
and annotate a PDF**: numbered page text, the paragraph around a passage, links
and the outline to follow citations and sections, and the PDF's annotations,
read and written. It knows nothing about viewers. Every tool takes the
document's `path`, and the page-scoped ones its `page`. A viewer server
supplies both and reports what you are reading:
[okular-mcp](https://github.com/david-wang-0/okular-mcp) for Okular, or
[vscode-pdf-mcp](https://github.com/david-wang-0/vscode-pdf-mcp) for a PDF
viewer inside VS Code.

The PDF file is the shared state. Viewers save their annotations as standard
PDF annots, and this server reads and writes those same annots with
[PyMuPDF](https://pymupdf.readthedocs.io/). Nothing lives in a private database,
and the notes travel with the file.

## Tools

| Tool | What it does |
|---|---|
| `page_text(path, page, to?, lines?)` | Numbered text lines of pages `page`..`to`. `lines` narrows each page to a window: `"12"` is one line, `"12-30"` a range, `"12-"` runs to the end of the page, `"-30"` runs from the start |
| `context(path, page, text, neighbours?)` | The paragraph that contains `text` (typically the user's selection), with neighbouring paragraphs and line numbers |
| `locate(path, page, text)` | Line numbers of `text` on the page, `{"page", "lines": "3-4"}`, or `null` when the text is not there. Use it to turn a viewer's selection into a place |
| `links(path, page)` | Links on a page: anchor text and line, then either the target page and line (internal links: citations, sections, figures) or the URL. The model can follow a citation itself with `page_text` |
| `outline(path)` | The bookmark tree (sections with page numbers), a map of the paper |
| `annotations(path, page?)` | All saved annotations, or only those on `page`: page, type, author, highlighted text, note |
| `mark_text(path, page, text, note?, style?, note_in_margin?, color?)` | Marks every occurrence of `text` on the page (quote enough words to be unique) as `highlight` (default), `underline`, `squiggly` or `strikeout`, saved incrementally into the PDF. The note sits on the mark, or, with `note_in_margin`, on a comment icon in the margin that replies to the mark |
| `add_note(path, note, page?, near_text?, reply_to?, color?)` | A comment icon in the margin, level with `near_text` or at the top of the page. With `reply_to` it is a threaded reply to that annotation, and the page is found automatically |
| `attach_file(path, name, content, page?, note?, near_text?, replace_xref?, color?)` | Embeds a text file (`notes.md`, `diagram.mmd`, …) as a paperclip annotation in the margin. With `replace_xref` it replaces that attachment's file, and the page is found automatically |
| `read_attachment(path, xref)` | The contents of a file-attachment annotation, decoded as text |
| `remove_annotation(path, xref)` | Deletes an annotation by the `xref` that `annotations` reports, with every reply to it, saved incrementally |
| `sync_to_source(path)` | Writes the PDF's highlights and notes into its LaTeX sources as `\pdfmark`/`\pdfnote` macros, so a rebuild keeps them, and reports what it inserted, updated and removed. See [LaTeX sources](#latex-sources) |

Pages are 1-based. A page outside the document raises `page N outside 1-M`; a
missing file raises `no such file: <path>`.

Marks written by the server carry a distinct author (`llm`, override with
`PDF_READER_WRITER_AUTHOR`) and a light-blue colour (blue for underlines and
notes, red for strikeout), which sets them apart from yours. Every writing tool
takes `color`: a palette name (`blue`, `yellow`, `green`, `orange`, `pink`,
`purple`, `red`, `cyan`, `grey`), `#rrggbb`, or `r,g,b` in 0–1. That lets a
session keep colours consistent by usage, for example one for definitions and
another for open questions.

## With a viewer

The viewer servers ([okular-mcp](https://github.com/david-wang-0/okular-mcp),
[vscode-pdf-mcp](https://github.com/david-wang-0/vscode-pdf-mcp)) share one
contract and only report what you are reading:

- `viewer_state()` gives the open documents with `path`, `page` and `pages`, the active one first.
- `get_selection()` gives the selected text and, when it can be attributed, its `path` and `page`.
- `goto(page, path?)` shows a page, when you ask to be shown a place.
- A `UserPromptSubmit` hook prepends `The user is reading <path> in <viewer>, page N of M.`
  to each prompt, so the model usually has `path` and `page` without a tool call.

The model takes `path` and `page` from there and calls this server for content:
`get_selection` then `locate` or `context` to see the passage you mean, `page_text`
to read on, `mark_text` or `add_note` to write back. Neither viewer server writes
to the PDF, and this server never talks to a viewer. Both viewers watch the file
and re-render when it changes, so there is no reload step.

## LaTeX sources

Marks in a PDF built from LaTeX vanish when it is rebuilt. `sync_to_source`
carries them into the sources as macros of the `pdfnotes` package (in
`latex/`), which turns them back into PDF annotations on every build. Every
author's marks are carried, yours, the model's and co-authors', each with its
`author=`:

- a highlight, underline, squiggle or strike-out becomes
  `\pdfmark[id=…,author=…,color=#rrggbb,style=…]{marked text}{note}` around the
  marked words in the source;
- a comment icon becomes `\pdfnote[id=…,author=…,color=#rrggbb]{note}` at the end
  of its source line, before a `%` comment;
- a reply becomes a `\pdfnote[…,replyto=<id>]` right after its parent's macro.

It needs a SyncTeX file next to the PDF (build with `-synctex=1`; for a symlinked
PDF, next to the file it points to will do), which maps each mark to its source
line, and `\usepackage{pdfnotes}` in the preamble. Only
`.tex` files in the main file's directory and below are edited, never the TeX
tree. Each changed file is replaced atomically, with its mode and line endings.
A symlinked `.tex` stays a symlink, and its target is replaced. The SyncTeX
lookups (one `synctex` process per mark) run in parallel without the PDF's lock.
The exclusive lock is held only while the sync reads the PDF afresh and writes
the sources, so a mark made meanwhile does not time out.

The round trip is idempotent. Sync stamps each annotation it wrote with
`/NM pdfnotes:<id>`. A rebuild re-emits the marks with the same `/NM`, author,
colour and note. The next sync finds every id in the sources and does nothing.
When you change a synced note, author, style or colour in the viewer, the next
sync rewrites only that part of the macro. Shorthands declared with
`\newpdfnote{\name}` or `\newpdfmark{\name}` are read like the macros they
stand for, and a hand-written macro without `id=` gets the id the package gave it.

Deletions propagate only when they are made through this package:
`remove_annotation`, or the edit service's `delete` (the VS Code viewer). Those
record the `pdfnotes` ids of the deleted thread in the PDF itself, under the
catalog key `/PDFNotesDeleted`, in the same save. The next sync removes their
macros, with their replies: a `\pdfnote` with the space before it, and a
`\pdfmark` unwrapped to its marked text. Then it clears the record. An id that
is back in the PDF (an undo, or a build from older sources) is kept and cleared
from the record. Undo through the edit service's `restore` brings back the
annotation with its id and clears the record, so the next sync writes the
macro back with the same id.

An id that is merely missing from the PDF deletes nothing. A build that read the
sources before a sync added a mark finishes without it, and that must not cost
the mark. The safe failure is this: a mark deleted in Okular is not recorded,
so the next build brings it back. Delete it through the VS Code viewer or the
tools instead, or delete its macro in the source.

Colours are compared only when the source gives `#rrggbb` or a palette name
(`blue`, `yellow`, …), within 1/255. Other xcolor names and expressions such as
`red!50` are left alone, and so is a colour or author the macro leaves to the
package default.

A mark whose text is not found in the source, within three lines of where
SyncTeX puts it, becomes a `\pdfnote` with the text `“marked text”: note` at
that line instead, and is listed under `fallback`. That happens when the marked
text crosses a blank line, a comment, a brace group or another macro, or comes
from a macro's expansion. Annotations that have no macro (file attachments,
free text, ink) are listed under `skipped`.

## Marking from the viewer

The user's own marks go through this package too. `pdf-reader-writer-edit` is a
long-lived JSON-lines service that a viewer spawns. The VS Code viewer of
vscode-pdf-mcp uses it to mark, note, reply, restyle, delete and undo. So the
user and the model write through the same code and produce the same standard
annotations. Every reader and writer, the MCP tools included, takes an `flock`
on the PDF, so concurrent writes from both sides are never lost. The protocol is
in [EDITING.md](EDITING.md).

## Notes from LaTeX as annotations

`latex/pdfnotes.sty` is a small LaTeX package for review notes in a paper's
sources. `\pdfnote[author=...,kind=todo]{...}` is a todonotes note that is also a
Text annotation over its box. `\pdfmark[...]{marked text}{note}` puts a highlight,
underline, squiggle or strike-out annotation over typeset text, across line and
page breaks. So LaTeX notes show up in every viewer's annotation list, and in
`annotations`, with author, colour, subject and note, while the page keeps the
todonotes look. A `disable` build has no notes and no annotations. Install,
usage, and the exact interface (keys, `/NM pdfnotes:<id>`, the Info key
`/PDFNotes`, normalisation rules) are in [latex/README.md](latex/README.md).

## Install

With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/david-wang-0/pdf-reader-writer-mcp
```

Then register it with Claude Code at user scope, so it is available in every
project, next to the viewer server you read with (okular-mcp or vscode-pdf-mcp,
registered the same way):

```sh
claude mcp add pdf-reader-writer -s user -- pdf-reader-writer-mcp
```

To limit it to one repository instead, run
`claude mcp add pdf-reader-writer -s project -- pdf-reader-writer-mcp` inside that
repository (written to its `.mcp.json`).

Codex CLI:

```sh
codex mcp add pdf-reader-writer -- pdf-reader-writer-mcp
```

Any other client: run `pdf-reader-writer-mcp` as a stdio server named `pdf-reader-writer`.

## Gotchas

- **Okular: save before any writing tool** (`mark_text`, `add_note`,
  `attach_file`, `remove_annotation`). Okular keeps annotations in memory until
  you save (Ctrl+S). If the file changes on disk while it has unsaved marks, it
  offers a reload, and those marks are lost if you accept. This server only sees
  what is in the file, so `annotations` misses unsaved marks too. The tool
  descriptions tell the model to ask you to save first. The VS Code viewer has
  no unsaved state.
- **Locking.** Writers take an exclusive `flock` on the PDF and give up after
  10 s with `PDF is locked by another writer`. Okular does not take the lock
  (it writes only when you save).
- **Lines are reconstructed.** A PDF has no lines. `page_text`, `context` and
  `locate` use PyMuPDF's block and line grouping, so a two-column page reads
  column by column, and equations or footnotes can land in odd places.
- **Search takes a selection as viewers give it.** `mark_text`, `context` and
  `locate` search the page's text layer, case-insensitively, first literally,
  then tolerantly: a quote may join lines with spaces or newlines, keep or drop
  a hyphen that ends a line (Okular keeps `dif-`/`ficult`, the VS Code viewer
  gives `difficult`), and spell a ligature (fi, fl, ffi) either way; spacing is
  not compared. An accented letter that the font extracts as two characters
  (an accent and the letter) still needs the page's spelling. For `context` and
  `locate`, a passage that is not found whole (a selection across columns or
  pages) is retried by its first six words.
- **Highlighted text** is recovered from the annotation's quads, so it can pick
  up a neighbouring word on tight line spacing.
- **The real `pdfnotes` needs two LaTeX runs.** Mark positions come from the
  `.aux`, like `\ref`. latexmk and LaTeX Workshop rerun by themselves.
- **Editors and `sync_to_source`.** A `.tex` file open in an editor with unsaved
  edits gets a conflict when sync changes it on disk. Save the sources before a
  sync, and save the PDF in Okular too.

## Development

```sh
uv venv && uv pip install -e '.[test]'
.venv/bin/pytest
```

`pdf_reader_writer_mcp/pdf.py` holds all PDF logic and the file lock; `source.py` the sync into LaTeX sources; `server.py` is the thin MCP layer over them and `edit.py` the edit service (`.venv/bin/pdf-reader-writer-edit`). The tests that build a PDF need `pdflatex` and `synctex` on `PATH`, and are skipped without them. `tests/test_latex.py` tests `latex/pdfnotes.sty` itself (also with lualatex and xelatex when installed).

## License

MIT
