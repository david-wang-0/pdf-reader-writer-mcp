import os

import pymupdf
import pytest

from pdf_reader_writer_mcp import pdf


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "s.pdf"
    doc = pymupdf.open()
    for n in (1, 2):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"Page {n} first line of text.")
        pg.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.")
    doc.save(path)
    doc.close()
    return str(path)


def test_page_text(sample):
    out = pdf.page_text(sample, 2)
    assert out.startswith("--- page 2 (2 lines) ---\n1: Page 2 first line") and "2: The quick" in out
    assert "Page 1" not in out
    assert "Page 1" in pdf.page_text(sample, 1, 2)
    assert pdf.page_text(sample, 1, lines="2-").splitlines()[1:] == ["2: The quick brown fox jumps over the lazy dog."]
    assert pdf.page_text(sample, 1, lines="-1").count("\n") == 1
    with pytest.raises(ValueError):
        pdf.page_text(sample, 3)
    with pytest.raises(ValueError):
        pdf.page_text(sample, 1, lines="x")


def test_context(tmp_path):
    path = tmp_path / "c.pdf"
    doc = pymupdf.open()
    pg = doc.new_page()
    y = 72
    for para in ("Intro para line one.\nIntro para line two.", "Body para says the fox\njumps high.", "Closing para here."):
        pg.insert_text((72, y), para)
        y += 60
    doc.save(path); doc.close()
    c = pdf.context(str(path), 1, "the  fox\njumps")
    assert c["paragraph"] == "Body para says the fox\njumps high."
    assert c["before"] == "Intro para line one.\nIntro para line two." and c["after"] == "Closing para here."
    assert c["lines"] == "3-4" and c["paragraph_lines"] == "3-4"
    assert pdf.context(str(path), 1, "fox", neighbours=0)["before"] == ""
    assert pdf.locate(str(path), 1, "fox\njumps") == {"page": 1, "lines": "3-4"}
    assert pdf.locate(str(path), 1, "absent words") is None
    assert pdf.locate(str(path), 1, "   ") is None
    with pytest.raises(ValueError, match="page 9 outside 1-1"):
        pdf.locate(str(path), 9, "fox")
    with pytest.raises(LookupError):
        pdf.context(str(path), 1, "not on this page at all")
    with pytest.raises(ValueError, match="text is empty"):
        pdf.context(str(path), 1, "  ")


def test_highlight_roundtrip(sample):
    r = pdf.add_highlight(sample, 1, "brown fox", note="check", author="tester")
    assert r["page"] == 1
    anns = pdf.annotations(sample)
    assert len(anns) == 1
    a = anns[0]
    assert a["type"] == "Highlight" and a["page"] == 1
    assert a["author"] == "tester" and a["note"] == "check"
    assert a["text"] == "brown fox"
    assert pdf.annotations(sample, page=2) == []


def test_highlight_missing(sample):
    with pytest.raises(LookupError):
        pdf.add_highlight(sample, 1, "no such words here")


def test_remove(sample):
    pdf.add_highlight(sample, 1, "quick brown")
    xref = pdf.add_highlight(sample, 2, "lazy dog")["xref"]
    assert len(pdf.annotations(sample)) == 2
    r = pdf.remove_annotation(sample, xref)
    assert r["page"] == 2 and r["type"] == "Highlight"
    left = pdf.annotations(sample)
    assert len(left) == 1 and left[0]["text"] == "quick brown"
    with pytest.raises(LookupError):
        pdf.remove_annotation(sample, xref)


def test_mark_styles_and_margin_note(sample):
    r = pdf.mark_text(sample, 1, "lazy dog", note="why lazy?", style="underline", note_in_margin=True)
    assert "note_xref" in r
    anns = {a["xref"]: a for a in pdf.annotations(sample)}
    mark, note = anns[r["xref"]], anns[r["note_xref"]]
    assert mark["type"] == "Underline" and mark["text"].startswith("lazy dog") and mark["note"] == ""
    assert note["type"] == "Text" and note["note"] == "why lazy?" and note["reply_to"] == r["xref"]
    with pytest.raises(ValueError):
        pdf.mark_text(sample, 1, "fox", style="bold")


def test_add_note_and_reply(sample):
    a = pdf.add_note(sample, 2, "page-level remark")
    b = pdf.add_note(sample, 2, "near the fox", near_text="brown fox")
    c = pdf.add_note(sample, None, "answering", reply_to=b["xref"])
    assert c["page"] == 2 and c["reply_to"] == b["xref"]
    anns = pdf.annotations(sample, page=2)
    assert [x["note"] for x in anns] == ["page-level remark", "near the fox", "answering"]
    with pytest.raises(LookupError):
        pdf.add_note(sample, None, "x", reply_to=999999)


def test_attachments(sample):
    md = "# Notes\n\n```mermaid\ngraph TD; A-->B;\n```\n"
    r = pdf.attach_file(sample, 1, "notes.md", md, note="summary", near_text="brown fox")
    assert r["bytes"] == len(md.encode())
    (a,) = pdf.annotations(sample)
    assert a["type"] == "FileAttachment" and a["file"] == "notes.md" and a["bytes"] == r["bytes"]
    back = pdf.read_attachment(sample, r["xref"])
    assert back["content"] == md and back["name"] == "notes.md" and back["note"] == "summary"
    r2 = pdf.attach_file(sample, None, "diagram.mmd", "graph LR; X-->Y;", replace_xref=r["xref"])
    assert r2["xref"] == r["xref"] and r2["page"] == 1
    back = pdf.read_attachment(sample, r["xref"])
    assert back["content"] == "graph LR; X-->Y;" and back["name"] == "diagram.mmd"
    h = pdf.mark_text(sample, 2, "lazy dog")
    with pytest.raises(ValueError):
        pdf.read_attachment(sample, h["xref"])


def test_replacing_an_attachment_without_note_clears_the_note(sample):
    """Regression: the replace path meant to clear the note but Annot.set_info skipped the empty string."""
    x = pdf.attach_file(sample, 1, "a.md", "old", note="about old")["xref"]
    pdf.attach_file(sample, None, "b.md", "new", replace_xref=x)
    assert pdf.read_attachment(sample, x)["note"] == ""


def test_colors(sample):
    assert pdf.parse_color(None, (1, 1, 1)) == (1, 1, 1)
    assert pdf.parse_color("Green", (0, 0, 0)) == pdf.COLORS["green"]
    assert pdf.parse_color("#ff0000", (0, 0, 0)) == (1.0, 0.0, 0.0)
    assert pdf.parse_color("0.5,0.25,0", (0, 0, 0)) == (0.5, 0.25, 0.0)
    for bad in ("chartreuse", "1,2,3", "#12345"):
        with pytest.raises(ValueError):
            pdf.parse_color(bad, (0, 0, 0))
    r = pdf.mark_text(sample, 1, "brown fox", color="yellow")
    yellow = [round(c, 2) for c in pdf.COLORS["yellow"]]
    assert r["color"] == yellow
    n = pdf.add_note(sample, 1, "n", color="#0000ff")
    u = pdf.mark_text(sample, 2, "brown fox", style="underline", color="0.5,0.25,0")
    by = {a["xref"]: a for a in pdf.annotations(sample)}
    assert by[r["xref"]]["color"] == yellow
    assert by[n["xref"]]["color"] == [0.0, 0.0, 1.0]
    assert by[u["xref"]]["color"] == [0.5, 0.25, 0.0]
    with pytest.raises(ValueError):
        pdf.mark_text(sample, 1, "fox", color="chartreuse")


def test_links_and_outline(tmp_path):
    path = tmp_path / "l.pdf"
    doc = pymupdf.open()
    doc.new_page(), doc.new_page()  # page objects are invalidated by new_page: fetch afterwards
    p1, p2 = doc[0], doc[1]
    p1.insert_text((72, 72), "See [1] and the site.")
    p1.insert_text((72, 100), "Second line.")
    p2.insert_text((72, 72), "Heading on page two.")
    p2.insert_text((72, 300), "[1] The cited work.")
    words = {w[4]: pymupdf.Rect(w[:4]) for w in p1.get_text("words")}
    p1.insert_link({"kind": pymupdf.LINK_GOTO, "from": words["[1]"], "page": 1, "to": pymupdf.Point(72, 290)})
    p1.insert_link({"kind": pymupdf.LINK_URI, "from": words["site."], "uri": "https://example.org"})
    doc.set_toc([[1, "Intro", 1], [2, "Sub", 2]])
    doc.save(path); doc.close()
    assert pdf.outline(str(path)) == [{"level": 1, "title": "Intro", "page": 1}, {"level": 2, "title": "Sub", "page": 2}]
    ls = pdf.links(str(path), 1)
    assert len(ls) == 2
    goto = next(l for l in ls if "page" in l)
    assert goto["text"] == "[1]" and goto["line"] == 1 and goto["page"] == 2 and goto["target_line"] == 2
    uri = next(l for l in ls if "url" in l)
    assert uri["url"] == "https://example.org" and uri["text"] == "site." and uri["line"] == 1
    assert pdf.page_text(str(path), 2, lines="2-2").endswith("2: [1] The cited work.")
    assert pdf.links(str(path), 2) == []


# --- edge cases and gaps ---

def test_parse_range_edges():
    assert pdf._parse_range(None, 7) == (1, 7) and pdf._parse_range("", 7) == (1, 7)
    assert pdf._parse_range("3-", 7) == (3, 7) and pdf._parse_range("-4", 7) == (1, 4)
    assert pdf._parse_range("2-99", 5) == (2, 5)  # hi clamped to the line count
    assert pdf._parse_range("4", 7) == (4, 4) and pdf._parse_range("4-4", 7) == (4, 4)
    for bad in ("3-1", "0-2", "a-b", "0", "x"):
        with pytest.raises(ValueError):
            pdf._parse_range(bad, 10)


def test_page_text_to_before_page_raises(sample):
    with pytest.raises(ValueError):
        pdf.page_text(sample, 2, to=1)
    with pytest.raises(ValueError):
        pdf.page_text(sample, 1, to=3)


@pytest.mark.parametrize("page", [0, 3])
def test_out_of_range_page_raises_value_error(sample, page):
    calls = [
        lambda: pdf.context(sample, page, "fox"),
        lambda: pdf.links(sample, page),
        lambda: pdf.mark_text(sample, page, "fox"),
        lambda: pdf.add_note(sample, page, "n"),
        lambda: pdf.attach_file(sample, page, "a.md", "x"),
    ]
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert pdf.annotations(sample) == []


def test_note_and_attachment_need_a_page_or_target(sample):
    with pytest.raises(ValueError, match="reply_to"):
        pdf.add_note(sample, None, "n")
    with pytest.raises(ValueError, match="replace_xref"):
        pdf.attach_file(sample, None, "a.md", "x")
    h = pdf.mark_text(sample, 1, "lazy dog")["xref"]
    with pytest.raises(ValueError, match="not a file attachment"):
        pdf.attach_file(sample, None, "a.md", "x", replace_xref=h)
    with pytest.raises(LookupError):
        pdf.attach_file(sample, None, "a.md", "x", replace_xref=999999)


def _annot_rects(path, page):
    with pymupdf.open(path) as doc:
        pg = doc[page - 1]
        return {a.xref: pymupdf.Rect(a.rect) for a in pg.annots()}, [b[:4] for b in pg.get_text("blocks")]


def test_attach_file_top_of_page_in_left_margin(sample):
    r = pdf.attach_file(sample, 2, "a.md", "x")
    assert r == {"page": 2, "xref": r["xref"], "name": "a.md", "bytes": 1, "note": ""}
    rects, blocks = _annot_rects(sample, 2)
    top = min(b[1] for b in blocks)
    assert rects[r["xref"]].x0 < min(b[0] for b in blocks) and abs(rects[r["xref"]].y0 - (top - 2)) < 1


def test_margin_notes_left_of_text_block(sample):
    m = pdf.mark_text(sample, 1, "lazy dog", note="n", note_in_margin=True)
    a = pdf.add_note(sample, 1, "near", near_text="brown fox")
    t = pdf.add_note(sample, 1, "top")
    rects, blocks = _annot_rects(sample, 1)
    left = min(b[0] for b in blocks)
    assert all(rects[x].x0 < left for x in (m["note_xref"], a["xref"], t["xref"]))
    # level with the marked line, not the top of the page
    assert rects[m["note_xref"]].y0 > rects[t["xref"]].y0


def test_highlight_across_line_break(tmp_path):
    """A mark over a wrapped phrase is one annot with one quad per line; its text is the whole words once, in order."""
    path = str(tmp_path / "w.pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Body para says the fox\njumps high over it.")
    doc.save(path); doc.close()
    r = pdf.mark_text(path, 1, "the fox\njumps")
    assert r["text"] == "the fox\njumps"
    with pymupdf.open(path) as doc:
        pg = doc[0]
        assert len(next(pg.annots()).vertices) == 8
    (a,) = pdf.annotations(path)
    assert a["text"] == "the fox jumps"


def test_mark_text_marks_every_occurrence_in_one_annot(tmp_path):
    path = str(tmp_path / "d.pdf")
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "A brown fox ran.")
    pg.insert_text((72, 200), "Then another brown fox sat.")
    doc.save(path); doc.close()
    pdf.mark_text(path, 1, "brown fox")
    (a,) = pdf.annotations(path)
    assert a["text"] == "brown fox brown fox"


def test_note_on_mark_without_margin(sample):
    r = pdf.mark_text(sample, 1, "lazy dog", note="on the mark", note_in_margin=False)
    assert "note_xref" not in r and r["note"] == "on the mark"
    (a,) = pdf.annotations(sample)
    assert a["note"] == "on the mark" and a["xref"] == r["xref"]


def test_margin_flag_without_note_adds_no_icon(sample):
    r = pdf.mark_text(sample, 1, "lazy dog", note_in_margin=True)
    assert "note_xref" not in r and len(pdf.annotations(sample)) == 1


def test_remove_annotation_explicit_page(sample):
    x = pdf.add_note(sample, 2, "n")["xref"]
    with pytest.raises(LookupError):
        pdf.remove_annotation(sample, x, page=1)
    assert pdf.remove_annotation(sample, x, page=2) == {"page": 2, "xref": x, "type": "Text",
                                                          "author": pdf.DEFAULT_AUTHOR, "replies": []}
    assert pdf.annotations(sample) == []


def test_annotations_modified_and_reply_to(sample):
    a = pdf.add_note(sample, 1, "root")
    b = pdf.add_note(sample, None, "reply", reply_to=a["xref"])
    by = {x["xref"]: x for x in pdf.annotations(sample)}
    assert all(x["modified"].startswith("D:") for x in by.values())
    assert "reply_to" not in by[a["xref"]] and by[b["xref"]]["reply_to"] == a["xref"]
    assert "text" not in by[a["xref"]]  # only markup annots carry the marked text


def test_links_named_destination(tmp_path):
    """A /GoTo link to a named destination reports page, dest name and the target line (PDF y flipped)."""
    path = str(tmp_path / "n.pdf")
    doc = pymupdf.open()
    doc.new_page(), doc.new_page()
    p1, p2 = doc[0], doc[1]
    p1.insert_text((72, 72), "See Section two here.")
    p2.insert_text((72, 72), "Heading on page two.")
    p2.insert_text((72, 300), "Target line.")
    h = p2.rect.height
    doc.xref_set_key(doc.pdf_catalog(), "Dests", f"<</sec2 [{doc.page_xref(1)} 0 R /XYZ 72 {h - 290} 0]>>")
    words = {w[4]: pymupdf.Rect(w[:4]) for w in p1.get_text("words")}
    p1.insert_link({"kind": pymupdf.LINK_NAMED, "from": words["Section"], "name": "sec2"})
    doc.save(path); doc.close()
    assert pdf.links(path, 1) == [{"text": "Section", "line": 1, "page": 2, "dest": "sec2", "target_line": 2}]


def test_default_author_from_env(sample, monkeypatch):
    import importlib
    monkeypatch.setenv("PDF_READER_WRITER_AUTHOR", "reviewer")
    try:
        importlib.reload(pdf)
        assert pdf.DEFAULT_AUTHOR == "reviewer"
        pdf.mark_text(sample, 1, "lazy dog")
        assert pdf.annotations(sample)[0]["author"] == "reviewer"
        monkeypatch.delenv("PDF_READER_WRITER_AUTHOR")
        importlib.reload(pdf)
        assert pdf.DEFAULT_AUTHOR == "llm"
    finally:
        monkeypatch.undo()
        importlib.reload(pdf)


@pytest.mark.parametrize("call", ["annotations", "remove_annotation"])
@pytest.mark.parametrize("page", [0, 3])
def test_annotations_out_of_range_page_raises_value_error(sample, call, page):
    with pytest.raises(ValueError, match=f"page {page} outside 1-2"):
        if call == "annotations":
            pdf.annotations(sample, page)
        else:
            pdf.remove_annotation(sample, 1, page=page)


def test_page_text_single_line(sample):
    """lines="2" is line 2 only; "2-", "-1" and "1-2" are ranges."""
    assert pdf.page_text(sample, 1, lines="2") == "--- page 1 (2 lines) ---\n2: The quick brown fox jumps over the lazy dog."
    assert pdf.page_text(sample, 1, lines="1").splitlines()[1:] == ["1: Page 1 first line of text."]
    assert pdf.page_text(sample, 1, 2, lines="1").count("\n1: Page") == 2
    assert pdf.page_text(sample, 1, lines="1-2").count("\n") == 2


def test_page_errors_name_the_page(sample):
    for call, page in [(lambda: pdf.page_text(sample, 3), 3), (lambda: pdf.page_text(sample, 1, to=5), 5),
                       (lambda: pdf.context(sample, 0, "fox"), 0), (lambda: pdf.links(sample, 4), 4),
                       (lambda: pdf.mark_text(sample, 3, "fox"), 3), (lambda: pdf.add_note(sample, 0, "n"), 0),
                       (lambda: pdf.attach_file(sample, 9, "a.md", "x"), 9), (lambda: pdf.locate(sample, 3, "fox"), 3)]:
        with pytest.raises(ValueError, match=f"^page {page} outside 1-2$"):
            call()
    with pytest.raises(ValueError, match="before page 2"):
        pdf.page_text(sample, 2, to=1)


def test_locate(sample):
    assert pdf.locate(sample, 1, "lazy dog") == {"page": 1, "lines": "2-2"}
    assert pdf.locate(sample, 2, "Page 2 first line\nof text. The quick") == {"page": 2, "lines": "1-2"}
    assert pdf.locate(sample, 1, "Page 2") is None  # on another page
    assert pdf.locate(sample, 1, "") is None


def test_locate_with_touching_line_boxes(tmp_path):
    """Regression: a hit went to every line whose box it touched, and tightly set lines
    touch, so a passage on line 2 was reported as lines 1-3."""
    path = str(tmp_path / "t.pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "aaa bbb\nficult to\nccc ddd", fontsize=10)
    doc.save(path); doc.close()
    with pymupdf.open(path) as doc:
        b1, b2 = [l["bbox"] for l in pdf._lines(doc[0])][:2]
        assert b1.intersects(b2)  # what the test is about
    assert pdf.locate(path, 1, "ficult to") == {"page": 1, "lines": "2-2"}
    c = pdf.context(path, 1, "ficult to", neighbours=0)
    assert c["lines"] == "2-2" and c["paragraph_lines"] == "1-3"
    assert pdf.locate(path, 1, "bbb\nficult") == {"page": 1, "lines": "1-2"}


@pytest.mark.parametrize("blank", [" ", "   ", "\n", "\t ", ""])
def test_blank_text_marks_nothing(sample, blank):
    """Regression: a blank quote was searched as such, and MuPDF found every space on the
    page: mark_text saved an invisible highlight over them."""
    with pytest.raises(ValueError, match="^text is empty"):
        pdf.mark_text(sample, 1, blank)
    if blank:
        with pytest.raises(ValueError, match="^text is empty"):
            pdf.add_note(sample, 1, "n", near_text=blank)
        with pytest.raises(ValueError, match="^text is empty"):
            pdf.attach_file(sample, 1, "a.md", "x", near_text=blank)
    assert pdf.annotations(sample) == []


def test_missing_file(tmp_path):
    gone = str(tmp_path / "gone.pdf")
    for call in (lambda: pdf.page_text(gone, 1), lambda: pdf.outline(gone), lambda: pdf.annotations(gone),
                 lambda: pdf.locate(gone, 1, "x"), lambda: pdf.mark_text(gone, 1, "x")):
        with pytest.raises(FileNotFoundError, match=f"no such file: {gone}"):
            call()


# --- files and annotations written by other programs ---

def test_a_repaired_pdf_is_read_but_not_written(sample):
    """A broken xref table is repaired in memory on open; an incremental save on top of
    it would be appended to bytes MuPDF no longer reads as they are, so writes refuse."""
    data = open(sample, "rb").read()
    i = data.rindex(b"startxref")
    broken = data[:i] + b"startxref\n9999999\n%%EOF\n"
    with open(sample, "wb") as f:
        f.write(broken)
    assert "lazy dog" in pdf.page_text(sample, 1) and pdf.annotations(sample) == []
    with pytest.raises(RuntimeError, match=r"^document cannot be saved incrementally \(encrypted or repaired\?\)$"):
        pdf.mark_text(sample, 1, "lazy dog")
    assert open(sample, "rb").read() == broken


def test_a_file_removed_during_a_write(sample, monkeypatch):
    real = pdf.add_markup

    def removed(*args, **kwargs):
        a = real(*args, **kwargs)
        os.remove(sample)
        return a
    monkeypatch.setattr(pdf, "add_markup", removed)
    with pytest.raises(RuntimeError, match="the PDF changed on disk while it was being written"):
        pdf.mark_text(sample, 1, "fox")
    assert not os.path.exists(sample)


def test_a_file_removed_after_the_existence_check(sample, monkeypatch, tmp_path):
    gone = str(tmp_path / "gone.pdf")
    monkeypatch.setattr(pdf.os.path, "isfile", lambda p: True)
    with pytest.raises(FileNotFoundError, match=f"^no such file: {gone}$"):
        pdf.page_text(gone, 1)


def _edit_doc(path, fn):
    with pymupdf.open(path) as doc:
        fn(doc)
        doc.saveIncr()


def test_marks_as_other_viewers_write_them(sample):
    """Okular and pdf.js write highlights with opacity, sometimes without /C; a quad may
    be given twice. The marked text is each word once; no colour means no "color"."""
    def write(doc):
        pg = doc[0]
        fox = pg.search_for("fox", quads=True)[0]
        h = pg.add_highlight_annot([fox, fox])
        h.set_opacity(0.4)
        h.update()
        u = pg.add_underline_annot(pg.search_for("lazy", quads=True))
        doc.xref_set_key(u.xref, "C", "null")
    _edit_doc(sample, write)
    h, u = pdf.annotations(sample)
    assert h["text"] == "fox" and "color" in h
    assert u["text"] == "lazy" and "color" not in u


def test_opacity_survives_restyle_delete_and_restore(sample):
    from pdf_reader_writer_mcp import edit

    def write(doc):
        pg = doc[0]
        h = pg.add_highlight_annot(pg.search_for("brown fox", quads=True))
        h.set_opacity(0.4)
        h.update()
        n = pg.add_text_annot((30, 30), "translucent note")
        n.set_opacity(0.5)
        n.update()
    _edit_doc(sample, write)
    h, n = (a["xref"] for a in pdf.annotations(sample))
    h = edit.update(sample, h, style="underline")["xref"]
    snaps = edit.delete(sample, h)["snapshots"] + edit.delete(sample, n)["snapshots"]
    assert [round(s["opacity"], 3) for s in snaps] == [0.4, 0.5]
    xrefs = edit.restore(sample, snaps)["xrefs"]
    with pymupdf.open(sample) as doc:
        pg = doc[0]  # an Annot must not outlive its Page
        back = {a.xref: a for a in pg.annots()}
        assert [round(back[x].opacity, 2) for x in xrefs.values()] == [0.4, 0.5]
        assert back[xrefs[str(h)]].type[1] == "Underline"


def test_a_reply_cycle_is_one_thread(sample):
    """Two notes replying to each other (a broken /IRT chain): deleting either takes both,
    and nothing loops."""
    a = pdf.add_note(sample, 1, "a")["xref"]
    b = pdf.add_note(sample, None, "b", reply_to=a)["xref"]
    with pdf._open(sample, write=True) as doc:
        doc.xref_set_key(a, "IRT", f"{b} 0 R")
    with pymupdf.open(sample) as doc:
        assert pdf.thread(doc, a) == [a, b] and pdf.thread(doc, b) == [b, a]
    r = pdf.remove_annotation(sample, a)
    assert r["replies"] == [b] and pdf.annotations(sample) == []


def test_links_to_odd_targets(tmp_path):
    """A link to a point below the last line goes to the last line, to an empty page to no
    line; two links to one page read it once; a launch link is no use to a reader."""
    path = str(tmp_path / "t.pdf")
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    p1, p2 = doc[0], doc[1]
    p1.insert_text((72, 72), "One two three four.")
    p2.insert_text((72, 72), "Only line on page two.")
    words = {w[4]: pymupdf.Rect(w[:4]) for w in p1.get_text("words")}
    p1.insert_link({"kind": pymupdf.LINK_GOTO, "from": words["One"], "page": 1, "to": pymupdf.Point(72, 700)})
    p1.insert_link({"kind": pymupdf.LINK_GOTO, "from": words["two"], "page": 1, "to": pymupdf.Point(72, 60)})
    p1.insert_link({"kind": pymupdf.LINK_GOTO, "from": words["three"], "page": 2, "to": pymupdf.Point(72, 60)})
    p1.insert_link({"kind": pymupdf.LINK_LAUNCH, "from": words["four."], "file": "other.pdf"})
    doc.save(path); doc.close()
    ls = pdf.links(path, 1)
    assert [(l["text"], l["page"], l["target_line"]) for l in ls] == [("One", 2, 1), ("two", 2, 1), ("three", 3, None)]
    assert all("url" not in l for l in ls)
