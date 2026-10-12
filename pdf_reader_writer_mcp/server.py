"""The MCP server: eleven tools over the pdf backend."""

from __future__ import annotations

import functools
from typing import Any, Callable

try:  # mcp >= 2: FastMCP was renamed MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

from mcp.types import ToolAnnotations

from . import pdf

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

mcp = FastMCP(
    "pdf-reader-writer",
    instructions=(
        "Reads and annotates PDF files. Every tool takes the document's `path`, and page-scoped "
        "tools its 1-based `page`: get both from a viewer server's viewer_state (okular-mcp, "
        "vscode-pdf-mcp), or from the \"The user is reading <path> in <viewer>, page N of M\" "
        "context the viewer's prompt hook injects. page_text and context read the page; locate "
        "turns the user's selection (a viewer's get_selection) into line numbers; annotations "
        "collects their highlights and notes; links and outline let you follow citations and "
        "sections yourself; mark_text, add_note and attach_file write marks, notes and files "
        "(e.g. Markdown, Mermaid) back. The viewers watch the file and show writes by themselves. "
        "Okular keeps unsaved annotations in memory and loses them when the file changes: when "
        "the user reads in Okular, ask them to save (Ctrl+S) before writing."
    ),
)


def tool(title: str, annotations: ToolAnnotations) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a tool whose anticipated failures reach the model as their message.

    mcp >= 2 shows the model only "Error executing tool <name>" for any exception
    but ToolError, which would hide "page 9 outside 1-4" or "text not found on page 3".
    """
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except (ValueError, LookupError, OSError, RuntimeError) as e:
                raise ToolError(str(e)) from e
        mcp.tool(title=title, annotations=annotations)(wrapper)
        return fn  # direct (Python) callers keep the original exception types
    return register


@tool(title="PDF: page text, numbered lines", annotations=READ)
def page_text(path: str, page: int, to: int | None = None, lines: str | None = None) -> str:
    """Numbered text lines of a page range.

    Numbered text lines of pages `page`..`to`; `lines` narrows to a window per page: "12" one line, "12-30", "12-" to the end, "-30" from the start. Line numbers match `context` and `locate`."""
    return pdf.page_text(path, page, to, lines)


@tool(title="PDF: paragraph around a passage", annotations=READ)
def context(path: str, page: int, text: str, neighbours: int = 1) -> dict[str, Any]:
    """The paragraph around a passage, e.g. the user's selection.

    The paragraph containing `text` (typically the user's selection from a viewer's get_selection) on `page`, with `neighbours` paragraphs before and after and the line numbers, so you can see what they are pointing at without reading the whole page."""
    return pdf.context(path, page, text, neighbours)


@tool(title="PDF: line numbers of a passage", annotations=READ)
def locate(path: str, page: int, text: str) -> dict[str, Any] | None:
    """Where a passage sits on a page, as line numbers.

    Line numbers of `text` (e.g. the user's selection) on `page`, as {"page", "lines": "3-4"} matching page_text and context; null when the text is not on that page (a selection may come from another window)."""
    return pdf.locate(path, page, text)


@tool(title="PDF: links on a page", annotations=READ)
def links(path: str, page: int) -> list[dict[str, Any]]:
    """Links on a page with their targets, to follow citations yourself.

    Links on `page`: anchor text and line, then the target page and line for internal links (citations, sections, figures) or the URL. Follow one with page_text(path, page=..., lines="<target_line>-...")."""
    return pdf.links(path, page)


@tool(title="PDF: table of contents", annotations=READ)
def outline(path: str) -> list[dict[str, Any]]:
    """The document's table of contents.

    The document's bookmark tree (sections with page numbers), a map of the paper."""
    return pdf.outline(path)


@tool(title="PDF: highlights and notes", annotations=READ)
def annotations(path: str, page: int | None = None) -> list[dict[str, Any]]:
    """All highlights, notes and attachments saved in the PDF.

    All annotations in the PDF, or on `page`: page, type, author, highlighted text, note. Only annotations saved to the file are visible (in Okular, ask the user to save first)."""
    return pdf.annotations(path, page)


@tool(title="PDF: highlight or underline text", annotations=WRITE)
def mark_text(path: str, page: int, text: str, note: str | None = None,
              style: str = "highlight", note_in_margin: bool = False,
              color: str | None = None) -> dict[str, Any]:
    """Highlight, underline, squiggle or strike out a passage, with a note.

    Mark every occurrence of `text` on `page` as highlight, underline, squiggly or strikeout, saved into the PDF under a distinct author; quote enough words to be unique. A note sits on the mark, or with note_in_margin on a comment icon in the margin replying to it (least intrusive). `color`: blue, yellow, green, orange, pink, purple, red, cyan, grey, #rrggbb or r,g,b in 0-1 (default light blue; red for strikeout). In Okular, the user must save first or their unsaved marks are lost."""
    return pdf.mark_text(path, page, text, note, style, note_in_margin, color=color)


@tool(title="PDF: margin note or reply", annotations=WRITE)
def add_note(path: str, note: str, page: int | None = None, near_text: str | None = None,
             reply_to: int | None = None, color: str | None = None) -> dict[str, Any]:
    """Put a comment icon in the margin, or reply to an annotation.

    Comment icon in the margin of `page`: level with `near_text`, else at the top of the page; or a threaded reply to the annotation `reply_to` (an xref from `annotations`, e.g. to answer the user's own note), whose page is found automatically, so `page` is then not needed. `color` as in mark_text. Same save-Okular-first caveat as mark_text."""
    return pdf.add_note(path, page, note, near_text, reply_to, color)


@tool(title="PDF: attach a text file", annotations=WRITE)
def attach_file(path: str, name: str, content: str, page: int | None = None,
                note: str | None = None, near_text: str | None = None,
                replace_xref: int | None = None, color: str | None = None) -> dict[str, Any]:
    """Embed a text file (Markdown, Mermaid) as a paperclip annotation.

    Embed a text file (e.g. notes.md, diagram.mmd) as a paperclip annotation in the margin of `page`, level with `near_text` or at the top of the page; or replace the file of an existing attachment `replace_xref`, whose page is found automatically, so `page` is then not needed. Same save-Okular-first caveat as mark_text."""
    return pdf.attach_file(path, page, name, content, note, near_text, replace_xref, color)


@tool(title="PDF: read an attached file", annotations=READ)
def read_attachment(path: str, xref: int) -> dict[str, Any]:
    """Read an attached file back as text.

    Contents of a file-attachment annotation (an xref from `annotations` with a `file` field), decoded as text."""
    return pdf.read_attachment(path, xref)


@tool(title="PDF: delete an annotation", annotations=DELETE)
def remove_annotation(path: str, xref: int) -> dict[str, Any]:
    """Delete an annotation, with its replies, by xref.

    Delete an annotation by the `xref` reported by `annotations`, together with every reply to it (recursively), and save. Same save-Okular-first caveat as mark_text."""
    return pdf.remove_annotation(path, xref)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
