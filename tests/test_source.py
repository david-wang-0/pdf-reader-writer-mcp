import os
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest

from pdf_reader_writer_mcp import edit, pdf, source
from pdf_reader_writer_mcp.source import Source, escape, find_text, note_point, scan, unescape

REPO = Path(__file__).resolve().parent.parent
STUB = Path(__file__).resolve().parent / "fixtures" / "pdfnotes.sty"
REAL = REPO / "latex" / "pdfnotes.sty"
NAMES = {"pdfnote": "note", "pdfmark": "mark"}

needs_tex = pytest.mark.skipif(not (shutil.which("pdflatex") and shutil.which("synctex")),
                               reason="needs pdflatex and synctex")
needs_package = pytest.mark.skipif(not REAL.exists(), reason="needs latex/pdfnotes.sty")


# --- escaping ---

@pytest.mark.parametrize("text", [
    "plain words", "50% & $5, #1, a_b, {x}", "back\\slash ~tilde^caret", "line one\nline two",
    "\nnew line first", "ünïcode “quotes” ✓",
])
def test_escape_round_trip(text):
    assert unescape(escape(text)) == text


def test_escape_spelling():
    assert escape("a\\b~c^d") == r"a\textbackslash{}b\textasciitilde{}c\textasciicircum{}d"
    assert escape("% & # _ { } $") == r"\% \& \# \_ \{ \} \$"
    assert escape("a\nb") == "a\\newline b"
    assert unescape(r"a\\b") == "a\nb" and unescape("a\nb") == "a b"
    assert unescape("a % comment\nb") == "a b" and unescape(r"\newlines") == r"\newlines"


# --- the scanner ---

def test_scanner_braces_comments_options():
    text = ("x \\pdfmark[id=a1, author={Wang, D}]{the {nested} \\{text}{a \\} note} y\n"
            "% \\pdfnote[id=no]{commented out}\n"
            "\\pdfnote {n} and 50\\% \\pdfnote[id=b2]{%\n  spans}\n")
    ms = scan(text, NAMES)
    assert [(m.kind, m.id) for m in ms] == [("mark", "a1"), ("note", None), ("note", "b2")]
    m = ms[0]
    assert m.opt("author") == "Wang, D"
    assert text[slice(*m.args[0])] == "the {nested} \\{text" and text[slice(*m.args[1])] == "a \\} note"
    assert text[m.start:m.end].endswith("note}") and text[m.end:m.end + 2] == " y"
    assert unescape(text[slice(*ms[2].args[0])]).strip() == "spans"


def test_scanner_shorthands():
    text = "\\newpdfnote{\\question}[kind=q]\n\\newpdfmark\\hl\n\\question{why?} \\hl{word}{n} \\questions{no}\n"
    names = {**NAMES, **source.aliases(text)}
    assert names["question"] == "note" and names["hl"] == "mark"
    assert [(m.name, m.kind) for m in scan(text, names)] == [("question", "note"), ("hl", "mark")]


def test_loads_package():
    assert source.loads_package("\\usepackage[draft]{xcolor,pdfnotes}")
    assert source.loads_package("\\usepackage{latex/pdfnotes}")
    assert not source.loads_package("% \\usepackage{pdfnotes}\n\\usepackage{pdfnotesx}")


# --- the text matcher ---

def src(text):
    s = Source("x.tex", text)
    s.macros = scan(text, NAMES)
    return s


def found(s, needle, line):
    span = find_text(s, needle, line)
    return span and s.text[span[0]:span[1]]


def test_match_spans_a_newline_and_hyphenation_and_ligature():
    s = src("The quick brown\n   fox jumps. An example of a\nfine flow.\n")
    assert found(s, "brown fox", 1) == "brown\n   fox"
    assert found(s, "An exam- ple", 2) == "An example"
    assert found(s, "ﬁne ﬂow", 3) == "fine flow"
    assert found(s, "well- known", 1) is None
    assert found(src("a well-known fact"), "well- known", 1) == "well-known"


def test_match_rejects_blank_line_comment_and_unbalanced_braces():
    assert found(src("brown\n\nfox"), "brown fox", 1) is None
    assert found(src("brown % c\nfox"), "brown fox", 1) is None
    assert found(src("\\emph{brown fox} jumps"), "fox jumps", 1) is None
    assert found(src("\\emph{brown fox} jumps"), "brown fox", 1) == "brown fox"


def test_match_closest_to_the_synctex_line_wins():
    s = src("the fox\nx\ny\nz\nthe fox\n")
    span = find_text(s, "the fox", 5)
    assert s.line_of(span[0]) == 5
    assert s.line_of(find_text(s, "the fox", 1)[0]) == 1


def test_match_skips_existing_macros():
    s = src("brown \\pdfnote[id=a]{x} fox \\pdfmark[id=b]{lazy dog}{n}\n")
    assert found(s, "brown fox", 1) is None  # would enclose the note
    assert found(s, "lazy dog", 1) is None  # already marked


def test_note_point_before_comment_and_after_blank_lines():
    s = src("some text   % a comment\n\n\nnext\n")
    p = note_point(s, 3)
    assert s.text[:p] == "some text"
    assert note_point(s, 4) == len("some text   % a comment\n\n\nnext")
    s = src("a \\pdfmark{long\nmarked}{n} rest\n")
    assert s.text[note_point(s, 1):].startswith(" rest")  # not inside the mark


# --- sync without TeX: a PyMuPDF page, a fake SyncTeX file ---

LINES = ["The quick brown fox jumps over the lazy dog.", "A second line with words."]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """doc.pdf with ``LINES``, doc.synctex naming doc.tex, and ``synctex edit``
    answering the PDF line under the point."""
    def make(tex, lines=LINES):
        texf = tmp_path / "doc.tex"
        texf.write_text(tex)
        doc = pymupdf.open()
        pg = doc.new_page()
        for i, line in enumerate(lines):
            pg.insert_text((72, 100 + 20 * i), line)
        doc.save(tmp_path / "doc.pdf"); doc.close()
        (tmp_path / "doc.synctex").write_text(f"SyncTeX Version:1\nInput:1:{texf}\nInput:2:/usr/share/x.cls\n")
        body = tex.split("\\begin{document}\n")[0].count("\n") + 2
        monkeypatch.setattr(source, "synctex_edit",
                            lambda path, page, x, y: [(str(texf), body + max(0, round((y - 95) / 20)))])
        return str(tmp_path / "doc.pdf"), texf
    return make


PRE = "\\documentclass{article}\n\\usepackage{pdfnotes}\n\\begin{document}\n"


def nm(path, xref):
    with pymupdf.open(path) as doc:
        return doc.xref_get_key(xref, "NM")[1]


def set_nm(path, xref, value):
    with pdf._open(path, write=True) as doc:
        doc.xref_set_key(xref, "NM", pymupdf.get_pdf_str(value))


def test_sync_wraps_and_stamps(fake):
    path, tex = fake(PRE + LINES[0] + "\n" + LINES[1] + "\n\\end{document}\n")
    x = pdf.mark_text(path, 1, "brown fox", note="50% sure", style="underline")["xref"]
    r = source.sync(path)
    (ins,) = r["inserted"]
    assert r["source"] == "doc.tex" and ins["file"] == "doc.tex" and ins["line"] == 4 and ins["macro"] == "pdfmark"
    assert f"\\pdfmark[id={ins['id']},author=llm,color=#3373ff,style=underline]{{brown fox}}{{50\\% sure}}" in tex.read_text()
    assert nm(path, x) == "pdfnotes:" + ins["id"]
    before = (tex.read_text(), Path(path).read_bytes())
    assert source.sync(path)["inserted"] == []
    assert (tex.read_text(), Path(path).read_bytes()) == before


def test_sync_reply_goes_after_its_parent(fake):
    path, tex = fake(PRE + "The quick \\pdfmark[id=p1]{brown fox}{} jumps over the lazy dog.\n\\end{document}\n")
    m = pdf.mark_text(path, 1, "brown fox")["xref"]
    set_nm(path, m, "pdfnotes:p1")
    pdf.add_note(path, None, "a reply", reply_to=m, author="me")
    r = source.sync(path)
    (ins,) = r["inserted"]
    assert f"{{brown fox}}{{}} \\pdfnote[id={ins['id']},author=me,color=#3373ff,replyto=p1]{{a reply}} jumps" in tex.read_text()


def test_sync_update_keeps_the_other_keys(fake):
    path, tex = fake(PRE + "The quick brown fox \\pdfnote[kind=q, id=n1 ,author=me,color=#ff0000]{old} jumps\n"
                     "\\pdfnote[id=n2,color=red!50]{same}\n\\end{document}\n")
    n = pdf.add_note(path, 1, "new\nnote", color="#ff0000", author="you")["xref"]
    set_nm(path, n, "pdfnotes:n1")
    n2 = pdf.add_note(path, 1, "same", color="green")["xref"]
    set_nm(path, n2, "pdfnotes:n2")
    r = source.sync(path)
    assert [(u["id"], u["changed"]) for u in r["updated"]] == [("n1", ["note", "author"])]
    text = tex.read_text()
    assert "\\pdfnote[kind=q, id=n1 ,author=you,color=#ff0000]{new\\newline note} jumps" in text
    assert "\\pdfnote[id=n2,color=red!50]{same}" in text  # an xcolor expression is left alone
    assert source.sync(path)["updated"] == []


def test_sync_deletes_only_when_notes_are_active(fake):
    tex_body = (PRE + "The quick \\pdfmark[id=gone,color=#ff0000]{brown {fox}}{n} jumps \\pdfnote[id=old]{x}\n"
                "\\pdfnote[id=keep]{k}\n\\end{document}\n")
    path, tex = fake(tex_body)
    assert source.sync(path)["removed"] == []  # no pdfnotes annotation: maybe a build without notes
    k = pdf.add_note(path, 1, "k")["xref"]
    set_nm(path, k, "pdfnotes:keep")
    r = source.sync(path)
    assert sorted(x["id"] for x in r["removed"]) == ["gone", "old"]
    assert "The quick brown {fox} jumps\n\\pdfnote[id=keep]{k}\n" in tex.read_text()


def test_notes_active_by_info_key(fake):
    path, _ = fake(PRE + "x\n\\end{document}\n")
    with pdf._open(path, write=True) as doc:
        assert not source.notes_active(doc)
        doc.set_metadata({"producer": "t"})  # makes sure there is an Info dictionary
        doc.xref_set_key(int(doc.xref_get_key(-1, "Info")[1].split()[0]), "PDFNotes", "(active)")
    with pdf._open(path) as doc:
        assert source.notes_active(doc)


def test_sync_adopts_an_implicit_id(fake):
    path, tex = fake(PRE + "The quick \\pdfmark{brown fox}{} jumps \\pdfmark[color=green]{over}{}\n\\end{document}\n")
    x = pdf.mark_text(path, 1, "brown fox")["xref"]
    set_nm(path, x, "pdfnotes:auto1")
    r = source.sync(path)
    assert r["inserted"] == [] and r["updated"][0]["changed"] == ["id"]
    assert "\\pdfmark[id=auto1]{brown fox}{} jumps \\pdfmark[color=green]{over}{}" in tex.read_text()


def test_sync_fallback_and_skipped(fake):
    path, tex = fake(PRE + "The quick brown % fox\nfox jumps over the lazy dog.\n\\end{document}\n",
                     ["The quick brown fox jumps over the lazy dog."])
    pdf.mark_text(path, 1, "brown fox", note="hm")
    a = pdf.attach_file(path, 1, "n.md", "body")["xref"]
    r = source.sync(path)
    (fb,) = r["fallback"]
    assert fb["text"] == "brown fox" and fb["macro"] == "pdfnote"
    assert f"The quick brown \\pdfnote[id={fb['id']},author=llm,color=#99ccff]{{“brown fox”: hm}} % fox" in tex.read_text()
    assert r["skipped"] == [{"xref": a, "page": 1, "subtype": "FileAttachment",
                             "reason": "FileAttachment annotations have no pdfnotes macro"}]
    assert source.sync(path)["updated"] == []  # the fallback note compares with its “text”: note


def test_fresh_ids_are_unique(fake, monkeypatch):
    path, tex = fake(PRE + "The quick brown fox \\pdfnote[id=aaaa0000]{x}\n\\end{document}\n")
    pdf.add_note(path, 1, "one")
    pdf.add_note(path, 1, "two")
    ids = iter(["aaaa0000", "bbbb1111", "bbbb1111", "cccc2222"])
    monkeypatch.setattr(source.secrets, "token_hex", lambda n: next(ids))
    r = source.sync(path)
    assert [i["id"] for i in r["inserted"]] == ["bbbb1111", "cccc2222"]


def test_crlf_and_mode_are_kept(fake):
    path, tex = fake(PRE + LINES[0] + "\n\\end{document}\n")
    tex.write_bytes(tex.read_bytes().replace(b"\n", b"\r\n"))
    os.chmod(tex, 0o640)
    pdf.mark_text(path, 1, "lazy dog")
    source.sync(path)
    raw = tex.read_bytes()
    assert b"{lazy dog}{}" in raw and raw.count(b"\r\n") == raw.count(b"\n")
    assert os.stat(tex).st_mode & 0o777 == 0o640


def test_no_synctex_and_no_package(tmp_path, fake):
    path, tex = fake(PRE.replace("\\usepackage{pdfnotes}\n", "% \\usepackage{pdfnotes}\n") + "x\n\\end{document}\n")
    with pytest.raises(ValueError, match=r"add \\usepackage\{pdfnotes\}"):
        source.sync(path)
    os.remove(tmp_path / "doc.synctex")
    before = Path(path).read_bytes()
    assert source.sync(path) == {"source": None, "reason": "no SyncTeX file next to the PDF (build with -synctex=1)"}
    assert Path(path).read_bytes() == before


def test_edit_op_sync(fake):
    path, tex = fake(PRE + LINES[0] + "\n\\end{document}\n")
    pdf.mark_text(path, 1, "lazy dog")
    resp = edit.handle({"id": 1, "op": "sync", "path": path})
    assert resp["ok"] and resp["result"]["inserted"][0]["macro"] == "pdfmark"
    assert edit.handle({"id": 2, "op": "sync"})["error"] == "missing field: path"


# --- with pdflatex and synctex ---

DOC = r"""\documentclass{article}
\usepackage{pdfnotes}
\newpdfnote{\question}
\begin{document}
The quick brown fox jumps over the lazy dog. \question{why?}

A second paragraph with a special sentence in it,
and it goes on for a while on a second source line.
\input{sec}
\end{document}
"""


def build(d, sty=STUB):
    shutil.copy(sty, d / "pdfnotes.sty")
    p = subprocess.run(["pdflatex", "-synctex=1", "-interaction=nonstopmode", "doc.tex"], cwd=d,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout[-2000:]
    return str(d / "doc.pdf")


@pytest.fixture
def paper(tmp_path):
    (tmp_path / "doc.tex").write_text(DOC)
    (tmp_path / "sec.tex").write_text("The section file has its own words.\n")
    return tmp_path


@needs_tex
def test_tex_highlight_round_trip(paper):
    path = build(paper)
    x = pdf.mark_text(path, 1, "brown fox")["xref"]
    r = source.sync(path)
    (ins,) = r["inserted"]
    assert ins == {"id": ins["id"], "file": "doc.tex", "line": 5, "macro": "pdfmark"}
    assert f"The quick \\pdfmark[id={ins['id']},author=llm,color=#99ccff]{{brown fox}}{{}} jumps" in (paper / "doc.tex").read_text()
    assert nm(path, x) == "pdfnotes:" + ins["id"]
    before = ((paper / "doc.tex").read_text(), Path(path).read_bytes())
    assert source.sync(path) == {**r, "inserted": []}
    assert ((paper / "doc.tex").read_text(), Path(path).read_bytes()) == before
    path = build(paper)  # the stub emits no annotations; the text survives
    assert "The quick brown fox jumps" in pdf.page_text(path, 1)


@needs_tex
def test_tex_note_reply_and_input_file(paper):
    path = build(paper)
    pdf.add_note(path, 1, "a note", near_text="special sentence")
    pdf.mark_text(path, 1, "own words", note="and a reply", note_in_margin=True)
    r = source.sync(path)
    by_macro = {(i["file"], i["macro"]): i for i in r["inserted"]}
    assert by_macro[("doc.tex", "pdfnote")]["line"] == 7
    mark, reply = by_macro[("sec.tex", "pdfmark")], by_macro[("sec.tex", "pdfnote")]
    assert f"\\pdfmark[id={mark['id']},author=llm,color=#99ccff]{{own words}}{{}} \\pdfnote[id={reply['id']},author=llm,color=#3373ff,replyto={mark['id']}]{{and a reply}}." in (paper / "sec.tex").read_text()
    assert "special sentence in it, \\pdfnote[" in (paper / "doc.tex").read_text()
    path = build(paper)
    assert "own words" in pdf.page_text(path, 1)


@needs_tex
def test_tex_changed_note_updates_the_source(paper):
    path = build(paper)
    x = pdf.mark_text(path, 1, "lazy dog", note="first")["xref"]
    nid = source.sync(path)["inserted"][0]["id"]
    edit.update(path, x, note="second & last")
    r = source.sync(path)
    assert r["updated"] == [{"id": nid, "file": "doc.tex", "line": 5, "macro": "pdfmark", "changed": ["note"]}]
    assert "{lazy dog}{second \\& last}" in (paper / "doc.tex").read_text()


@needs_tex
def test_tex_without_package_or_synctex(paper):
    (paper / "doc.tex").write_text(DOC.replace("\\usepackage{pdfnotes}\n\\newpdfnote{\\question}\n", "")
                                   .replace(" \\question{why?}", ""))
    path = build(paper)
    with pytest.raises(ValueError, match="does not load the pdfnotes package"):
        source.sync(path)
    os.remove(paper / "doc.synctex.gz")
    assert source.sync(path)["source"] is None


# --- with the real package ---

@needs_tex
@needs_package
def test_package_round_trip(paper):
    path = build(paper, REAL)
    pdf.mark_text(path, 1, "brown fox", note="a mark", color="green")
    pdf.add_note(path, 1, "a note", near_text="special sentence", author="me")
    r = source.sync(path)
    ids = {i["id"] for i in r["inserted"]}
    path = build(paper, REAL)
    with pymupdf.open(path) as doc:
        back = {a.info["id"][len(source.NM_PREFIX):]: a for pg in doc for a in pg.annots()
                if a.info["id"].startswith(source.NM_PREFIX)}
        assert set(back) == ids
        notes = {a.info["content"]: (a.info["title"], tuple(round(c, 2) for c in a.colors["stroke"]))
                 for a in back.values()}
    assert notes["a mark"] == ("llm", tuple(round(c, 2) for c in pdf.COLORS["green"]))
    assert notes["a note"][0] == "me"
    again = source.sync(path)
    assert again["inserted"] == again["updated"] == again["removed"] == again["fallback"] == []
    gone = next(a["xref"] for a in pdf.annotations(path) if a["note"] == "a note")
    pdf.remove_annotation(path, gone)
    r = source.sync(path)
    assert [x["macro"] for x in r["removed"]] == ["pdfnote"]
    assert "a note" not in (paper / "doc.tex").read_text()
