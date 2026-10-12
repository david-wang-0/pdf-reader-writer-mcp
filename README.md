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
| `remove_annotation(path, xref)` | Deletes an annotation by the `xref` that `annotations` reports, saved incrementally |

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
- **Lines are reconstructed.** A PDF has no lines. `page_text`, `context` and
  `locate` use PyMuPDF's block and line grouping, so a two-column page reads
  column by column, and equations or footnotes can land in odd places.
- **Search is literal.** `mark_text`, `context` and `locate` search the page's
  text layer. Ligatures (fi, fl, ff) and accented letters in some fonts do not
  match their plain spelling, so quote a plain-ASCII span instead. Whitespace
  and line breaks in the quote are normalised. For `context` and `locate`, a
  passage that is not found whole (a selection across columns or pages) is
  retried by its first six words.
- **Highlighted text** is recovered from the annotation's quads, so it can pick
  up a neighbouring word on tight line spacing.

## Development

```sh
uv venv && uv pip install -e '.[test]'
.venv/bin/pytest
```

`pdf_reader_writer_mcp/pdf.py` holds all PDF logic; `server.py` is the thin MCP layer over it.

## License

MIT
