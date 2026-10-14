"""The real server over stdio, as an MCP client (Claude Code, Codex) sees it: the
``mcp`` package's stdio client talking to ``python -m pdf_reader_writer_mcp.server``."""

import asyncio
import json
import os
import sys

import pymupdf
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from pdf_reader_writer_mcp import server

TOOLS = {"page_text", "context", "locate", "links", "outline", "annotations", "mark_text",
         "add_note", "attach_file", "read_attachment", "remove_annotation", "sync_to_source"}
READ = {"page_text", "context", "locate", "links", "outline", "annotations", "read_attachment"}


def run(body):
    """Start the server, initialise a session and run ``await body(session, init)``."""
    async def main():
        # the whole environment, not the SDK's minimal default: coverage measures the server through it
        params = StdioServerParameters(command=sys.executable, args=["-m", "pdf_reader_writer_mcp.server"],
                                       env=dict(os.environ))
        with open("/dev/null", "w") as errlog:  # the server logs failed calls on stderr
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w, read_timeout_seconds=60) as s:
                    return await body(s, await s.initialize())
    return asyncio.run(main())


def dump(x):
    return x.model_dump(by_alias=True, exclude_none=True)


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "s.pdf"
    doc = pymupdf.open()
    for n in (1, 2):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"Page {n} first line of text.")
        pg.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.")
    doc.save(path); doc.close()
    return str(path)


def test_initialize_and_tool_list():
    async def body(s, init):
        return init, (await s.list_tools()).tools
    init, tools = run(body)
    info = dump(init)
    assert info["serverInfo"]["name"] == "pdf-reader-writer"
    assert info["capabilities"]["tools"] is not None
    instructions = info["instructions"]
    assert instructions == server.mcp.instructions
    for phrase in ("`path`", "1-based `page`", "viewer_state", "get_selection", "okular-mcp",
                   "vscode-pdf-mcp", "The user is reading <path> in <viewer>, page N of M",
                   "sync_to_source", "save (Ctrl+S) before writing"):
        assert phrase in instructions, phrase

    by = {t.name: dump(t) for t in tools}
    assert set(by) == TOOLS
    for name, t in by.items():
        assert t["title"].startswith("PDF: "), name
        assert t["description"].split("\n")[0].endswith("."), name
        assert t["inputSchema"]["type"] == "object" and "path" in t["inputSchema"]["required"], name
        assert t["inputSchema"]["properties"]["path"]["type"] == "string", name
        hints = t["annotations"]
        assert hints["readOnlyHint"] is (name in READ), name
    required = {n: set(t["inputSchema"]["required"]) for n, t in by.items()}
    assert required == {
        "page_text": {"path", "page"}, "context": {"path", "page", "text"},
        "locate": {"path", "page", "text"}, "links": {"path", "page"}, "outline": {"path"},
        "annotations": {"path"}, "mark_text": {"path", "page", "text"}, "add_note": {"path", "note"},
        "attach_file": {"path", "name", "content"}, "read_attachment": {"path", "xref"},
        "remove_annotation": {"path", "xref"}, "sync_to_source": {"path"},
    }
    for n in ("page_text", "context", "locate", "links", "annotations", "mark_text", "add_note", "attach_file"):
        page = by[n]["inputSchema"]["properties"]["page"]
        assert page.get("type") == "integer" or {"type": "integer"} in page.get("anyOf", []), n
    assert {n for n, t in by.items() if t["annotations"].get("destructiveHint")} == {"remove_annotation", "sync_to_source"}
    assert by["sync_to_source"]["annotations"]["idempotentHint"] is True
    for n in ("mark_text", "add_note", "attach_file"):
        assert by[n]["annotations"]["destructiveHint"] is False and by[n]["annotations"]["idempotentHint"] is False
    assert all("Okular" in by[n]["description"] for n in ("mark_text", "annotations", "sync_to_source"))


def test_results_as_the_client_gets_them(sample, tmp_path):
    """Strings come as text, dicts and lists as JSON text, all also as ``structuredContent``:
    a dict as it is, anything else (a string, a list, locate's dict or null) as its ``result``.
    A list is one text item per element, so an empty list, like null, has no text at all."""
    async def body(s, init):
        out = {}
        for key, name, args in [
            ("page_text", "page_text", {"path": sample, "page": 2, "lines": "2"}),
            ("locate", "locate", {"path": sample, "page": 1, "text": "lazy dog"}),
            ("locate_miss", "locate", {"path": sample, "page": 1, "text": "not on this page"}),
            ("context", "context", {"path": sample, "page": 1, "text": "lazy dog", "neighbours": 0}),
            ("outline", "outline", {"path": sample}),
            ("links", "links", {"path": sample, "page": 1}),
            ("mark", "mark_text", {"path": sample, "page": 1, "text": "brown fox", "note": "n",
                                   "note_in_margin": True, "color": "green"}),
            ("note", "add_note", {"path": sample, "note": "top", "page": 2}),
            ("attach", "attach_file", {"path": sample, "name": "a.md", "content": "# A\n", "page": 2}),
            ("annotations", "annotations", {"path": sample, "page": 1}),
            ("sync", "sync_to_source", {"path": sample}),
        ]:
            out[key] = dump(await s.call_tool(name, args))
        x = out["attach"]["structuredContent"]["xref"]
        out["read"] = dump(await s.call_tool("read_attachment", {"path": sample, "xref": x}))
        m = out["mark"]["structuredContent"]["xref"]
        out["remove"] = dump(await s.call_tool("remove_annotation", {"path": sample, "xref": m}))
        return out
    out = run(body)
    assert not any(r["isError"] for r in out.values())

    wrapped = {"page_text", "locate", "locate_miss", "outline", "links", "annotations"}

    def result(key):
        r = out[key]
        value = r["structuredContent"]["result"] if key in wrapped else r["structuredContent"]
        texts = [c["text"] for c in r["content"]]
        if isinstance(value, list):  # one text item per element
            assert [json.loads(t) for t in texts] == value, key
        elif value is None:
            assert texts == [], key
        else:
            (text,) = texts
            assert (text if isinstance(value, str) else json.loads(text)) == value, key
        return value
    assert result("page_text") == "--- page 2 (2 lines) ---\n2: The quick brown fox jumps over the lazy dog."
    assert result("locate") == {"page": 1, "lines": "2-2"}
    assert result("locate_miss") is None and out["locate_miss"]["content"] == []
    c = result("context")
    assert set(c) == {"page", "selection", "lines", "paragraph_lines", "before", "paragraph", "after"}
    assert result("outline") == [] and result("links") == []
    mark = result("mark")
    assert set(mark) == {"page", "xref", "style", "text", "note", "author", "color", "note_xref"}
    assert mark["author"] == "llm" and mark["color"] == [0.6, 0.9, 0.6]
    assert set(result("note")) == {"page", "xref", "note", "author", "reply_to"}
    assert set(result("attach")) == {"page", "xref", "name", "bytes", "note"}
    anns = result("annotations")
    assert [a["type"] for a in anns] == ["Highlight", "Text"] and anns[1]["reply_to"] == mark["xref"]
    assert set(anns[0]) == {"page", "type", "author", "note", "modified", "xref", "text", "color"}
    assert result("read")["content"] == "# A\n"
    assert set(result("read")) == {"page", "xref", "name", "note", "author", "bytes", "content"}
    assert result("remove") == {"page": 1, "xref": mark["xref"], "type": "Highlight", "author": "llm",
                                "replies": [mark["note_xref"]]}
    assert result("sync") == {"source": None, "reason": "no SyncTeX file next to the PDF (build with -synctex=1)"}


def test_errors_arrive_as_their_message(sample, tmp_path):
    """Anticipated failures reach the model as their own text, after the SDK's prefix."""
    gone = str(tmp_path / "gone.pdf")
    tex = tmp_path / "s.tex"
    tex.write_text("\\documentclass{article}\n")
    cases = [
        ("page_text", {"path": sample, "page": 3}, "page 3 outside 1-2"),
        ("page_text", {"path": sample, "page": 1, "lines": "x"}, "lines must look like '12', '12-30', '12-' or '-30', not 'x'"),
        ("page_text", {"path": sample, "page": 2, "to": 1}, "to=1 is before page 2"),
        ("context", {"path": sample, "page": 1, "text": "words from elsewhere"}, "selection not found on page 1: 'words from elsewhere'"),
        ("context", {"path": sample, "page": 1, "text": " "}, "text is empty: pass the user's selection or a quote from the page"),
        ("outline", {"path": gone}, f"no such file: {gone}"),
        ("mark_text", {"path": sample, "page": 1, "text": "absent"}, "text not found on page 1: 'absent'"),
        ("mark_text", {"path": sample, "page": 1, "text": "fox", "style": "bold"}, "style must be one of highlight, underline, squiggly, strikeout"),
        ("mark_text", {"path": sample, "page": 1, "text": "fox", "color": "teal"}, "unknown colour 'teal': use one of blue, yellow"),
        ("add_note", {"path": sample, "note": "n"}, "page is required unless reply_to is given"),
        ("add_note", {"path": sample, "note": "n", "reply_to": 4242}, "no annotation with xref 4242"),
        ("attach_file", {"path": sample, "name": "a", "content": "x"}, "page is required unless replace_xref is given"),
        ("read_attachment", {"path": sample, "xref": 4242}, "no annotation with xref 4242"),
        ("remove_annotation", {"path": sample, "xref": 4242}, "no annotation with xref 4242"),
    ]

    async def body(s, init):
        out = [dump(await s.call_tool(n, a)) for n, a, _ in cases]
        (tmp_path / "s.synctex").write_text(f"SyncTeX Version:1\nInput:1:{tex}\n")
        out.append(dump(await s.call_tool("sync_to_source", {"path": sample})))
        out.append(dump(await s.call_tool("page_text", {"path": sample})))
        out.append(dump(await s.call_tool("no_such_tool", {})))
        return out
    out = run(body)
    for (name, _, message), r in zip(cases, out):
        assert r["isError"] is True, name
        (c,) = r["content"]
        assert c["text"].startswith(f"Error executing tool {name}: {message}"), c["text"]
    sync, missing, unknown = out[len(cases):]
    assert sync["isError"] and sync["content"][0]["text"].endswith(
        f"{tex} does not load the pdfnotes package: add \\usepackage{{pdfnotes}} to its preamble")
    assert missing["isError"] and "page" in missing["content"][0]["text"] and "Field required" in missing["content"][0]["text"]
    assert unknown["isError"] and unknown["content"][0]["text"] == "Unknown tool: no_such_tool"
