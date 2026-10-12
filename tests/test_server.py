import asyncio
import re
from pathlib import Path

import pymupdf
import pytest

from pdf_reader_writer_mcp import pdf, server


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


# --- read tools ---

def test_page_text_reads_the_given_page(sample):
    assert server.page_text(sample, 2).startswith("--- page 2 ")
    assert server.page_text(sample, 1, lines="2").endswith("\n2: The quick brown fox jumps over the lazy dog.")


def test_context_and_locate_agree_on_lines(sample):
    c = server.context(sample, 2, "lazy dog")
    assert c["page"] == 2 and c["selection"] == "lazy dog" and c["lines"] == "2-2"
    assert server.locate(sample, 2, "lazy dog") == {"page": 2, "lines": "2-2"}
    assert server.locate(sample, 2, "words from another window") is None


def test_outline_annotations_read_attachment(sample):
    x = pdf.attach_file(sample, 1, "n.md", "body")["xref"]
    assert server.outline(sample) == []
    assert [a["xref"] for a in server.annotations(sample)] == [x]
    assert server.annotations(sample, 2) == []
    assert server.read_attachment(sample, x)["content"] == "body"


def test_links_passes_path_and_page(sample, monkeypatch):
    seen = []
    monkeypatch.setattr(pdf, "links", lambda p, n: seen.append((p, n)) or [])
    server.links(sample, 2)
    assert seen == [(sample, 2)]


# --- write tools ---

def test_write_tools_write_to_the_file(sample):
    m = server.mark_text(sample, 2, "brown fox")
    n = server.add_note(sample, "n", page=2)
    a = server.attach_file(sample, "a.md", "x", page=2)
    assert m["page"] == n["page"] == a["page"] == 2
    assert [x["xref"] for x in pdf.annotations(sample)] == [m["xref"], n["xref"], a["xref"]]
    assert server.remove_annotation(sample, n["xref"])["page"] == 2
    assert [x["xref"] for x in pdf.annotations(sample)] == [m["xref"], a["xref"]]


def test_mark_text_passes_style_note_and_color(sample):
    r = server.mark_text(sample, 2, "lazy dog", note="hm", style="underline", note_in_margin=True, color="green")
    assert r["page"] == 2 and r["style"] == "underline" and "note_xref" in r
    assert r["color"] == [round(c, 2) for c in pdf.COLORS["green"]]


def test_add_note_reply_finds_target_page(sample):
    target = pdf.add_note(sample, 2, "question")["xref"]
    r = server.add_note(sample, "answer", reply_to=target)
    assert r["page"] == 2 and r["reply_to"] == target
    assert {a["xref"]: a["page"] for a in pdf.annotations(sample)}[r["xref"]] == 2


def test_add_note_near_text(sample):
    assert server.add_note(sample, "n", page=2, near_text="lazy dog")["page"] == 2


def test_add_note_and_attach_file_need_page_without_target(sample):
    with pytest.raises(ValueError, match="page is required unless reply_to"):
        server.add_note(sample, "n")
    with pytest.raises(ValueError, match="page is required unless replace_xref"):
        server.attach_file(sample, "a.md", "x")


def test_attach_file_replace_finds_target_page(sample):
    x = pdf.attach_file(sample, 2, "a.md", "old")["xref"]
    r = server.attach_file(sample, "b.md", "new", replace_xref=x)
    assert r["page"] == 2 and r["xref"] == x
    assert pdf.read_attachment(sample, x)["content"] == "new"


# --- errors reach the model ---

def _call(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


@pytest.mark.parametrize("name,args,message", [
    ("page_text", {"page": 3}, "page 3 outside 1-2"),
    ("mark_text", {"page": 1, "text": "not on the page"}, "text not found on page 1"),
    ("add_note", {"note": "n"}, "page is required unless reply_to"),
    ("remove_annotation", {"xref": 999999}, "no annotation with xref 999999"),
])
def test_tool_errors_carry_their_message(sample, name, args, message):
    """mcp >= 2 hides the message of anything but ToolError; the server converts anticipated failures."""
    with pytest.raises(server.ToolError, match=message) as e:
        _call(name, {"path": sample, **args})
    assert type(e.value) is server.ToolError  # not UnexpectedToolError, whose message is withheld


def test_missing_file_message(tmp_path):
    gone = str(tmp_path / "gone.pdf")
    with pytest.raises(server.ToolError, match=f"no such file: {gone}"):
        _call("outline", {"path": gone})


def test_call_tool_succeeds_through_the_wrapper(sample):
    out = _call("locate", {"path": sample, "page": 1, "text": "lazy dog"})
    assert "2-2" in str(out)


# --- registration ---

def _tools():
    return {t.name: t for t in asyncio.run(server.mcp.list_tools())}


def _schema(tool):
    return getattr(tool, "input_schema", None) or tool.inputSchema  # mcp >= 2 renamed the field


TOOLS = {"page_text", "context", "locate", "links", "outline", "annotations", "mark_text",
         "add_note", "attach_file", "read_attachment", "remove_annotation"}


def test_server_name():
    assert server.mcp.name == "pdf-reader-writer"


def test_registered_tools_match_readme_table():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    documented = set(re.findall(r"^\| `(\w+)\(", readme, re.M))
    assert set(_tools()) == documented == TOOLS


def test_path_required_everywhere_page_required_when_page_scoped():
    """No viewer coupling: the caller always names the document, and the page where one is meant."""
    required = {n: set(_schema(t).get("required", [])) for n, t in _tools().items()}
    assert all("path" in r for r in required.values())
    page_scoped = {"page_text", "context", "locate", "links", "mark_text"}
    assert {n for n, r in required.items() if "page" in r} == page_scoped
    # add_note/attach_file find their own page from reply_to/replace_xref; annotations defaults to all pages
    for n in ("add_note", "attach_file", "annotations"):
        assert "page" in _schema(_tools()[n])["properties"]
    assert {"text"} <= required["context"] and {"text"} <= required["locate"]


def test_tool_annotation_hints():
    hints = {n: t.annotations.model_dump(by_alias=True) for n, t in _tools().items()}
    read = {"page_text", "context", "locate", "links", "outline", "annotations", "read_attachment"}
    assert {n for n, h in hints.items() if h["readOnlyHint"]} == read
    assert {n for n, h in hints.items() if h["destructiveHint"]} == {"remove_annotation"}
    for n in ("mark_text", "add_note", "attach_file"):
        assert hints[n]["readOnlyHint"] is False and hints[n]["destructiveHint"] is False


def test_docstrings_are_viewer_neutral():
    """Okular is named only for its save-before-writing caveat, never as the source of path or page."""
    for n, t in _tools().items():
        desc = t.description or ""
        assert "page shown" not in desc and "default: the" not in desc, n
        assert "Okular" not in desc or "save" in desc.lower(), n
