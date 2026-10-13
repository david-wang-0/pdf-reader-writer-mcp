"""latex/pdfnotes.sty: LaTeX notes and marks that are also PDF annotations.

Small documents are compiled with pdflatex (lualatex and xelatex where installed) into a
temporary directory, with the package found through TEXINPUTS, and read back with PyMuPDF
and with this package's own `annotations`. Skipped without pdflatex.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pymupdf
import pytest

from pdf_reader_writer_mcp import pdf

LATEX_DIR = Path(__file__).resolve().parent.parent / "latex"
pytestmark = pytest.mark.skipif(not shutil.which("pdflatex"), reason="pdflatex is not installed")

PERSONS = r"""
\newpdfnote{\todoalice}{author=Alice,color=pink,kind=todo}
\newpdfnote{\commentalice}{author=Alice,color=cyan,kind=comment,prefix={Alice: }}
\newpdfnote{\todobob}{author=Bob,color=orange,kind=todo,prefix={Bob: }}
\newpdfnote{\donealice}{author=Alice,color=green,kind=done}
\newpdfmark{\flagalice}{author=Alice,color=pink,textcolor=orange,kind=flag}
"""


def build(tmp_path: Path, body: str, preamble: str = "", *, name: str = "doc", engine: str = "pdflatex",
          todonotes: str = r"\usepackage{todonotes}", options: str = "", classopts: str = "",
          runs: int = 2) -> tuple[str, str]:
    """Compile a document `runs` times; the PDF's path and the last run's log."""
    fontenc = r"\usepackage[T1]{fontenc}" if engine == "pdflatex" else ""
    tex = "\n".join([
        rf"\documentclass[{classopts}]{{article}}", fontenc, todonotes, rf"\usepackage[{options}]{{pdfnotes}}",
        preamble, r"\begin{document}", body, r"\end{document}", ""])
    (tmp_path / f"{name}.tex").write_text(tex, encoding="utf-8")
    env = dict(os.environ, TEXINPUTS=f"{LATEX_DIR}//:")
    for _ in range(runs):
        r = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error", f"{name}.tex"],
                           cwd=tmp_path, env=env, capture_output=True, text=True, errors="replace", timeout=180)
        assert r.returncode == 0, r.stdout[-3000:]
    return str(tmp_path / f"{name}.pdf"), (tmp_path / f"{name}.log").read_text(errors="replace")


def raw(doc: pymupdf.Document, xref: int, key: str) -> str | None:
    kind, val = doc.xref_get_key(xref, key)
    return None if kind == "null" else val


def rgb(c: str | None) -> list[float]:
    return [float(v) for v in (c or "").strip("[]").split()]


def close(a: list[float], b: tuple[float, ...] | list[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= 1 / 255 for x, y in zip(a, b))


def info(doc: pymupdf.Document, key: str) -> str | None:
    kind, val = doc.xref_get_key(-1, "Info")
    if kind != "xref":
        return None
    return raw(doc, int(val.split()[0]), key)


def by_id(path: str) -> dict[str, dict]:
    """`annotations` entries keyed by the id in their /NM (pdfnotes:<id>), with subject and NM."""
    out: dict[str, dict] = {}
    with pymupdf.open(path) as doc:
        for e in pdf.annotations(path):
            nm = raw(doc, e["xref"], "NM") or ""
            e["nm"] = nm
            e["subject"] = raw(doc, e["xref"], "Subj") or ""
            e["C"] = raw(doc, e["xref"], "C")
            out.setdefault(nm.removeprefix("pdfnotes:"), e)
    return out


def words_in_quads(page: pymupdf.Page, quads: list[pymupdf.Quad]) -> list[int]:
    """Indices of the page's words whose centre lies in one of the quads."""
    rects = [q.rect for q in quads]
    out = []
    for i, w in enumerate(page.get_text("words", sort=False)):
        c = pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
        if any(r.contains(c) for r in rects):
            out.append(i)
    return out


def span(page: pymupdf.Page, first: str, last: str) -> list[int]:
    """Indices of the page's words from the word `first` to the word `last` (content order)."""
    words = [w[4] for w in page.get_text("words", sort=False)]
    a = words.index(first)
    b = len(words) - 1 - words[::-1].index(last)
    return list(range(a, b + 1))


# ---- notes ------------------------------------------------------------------------------

def test_notes_per_person_commands(tmp_path):
    path, log = build(tmp_path, r"""
Reachability of timed automata is decidable via regions.
\todoalice{Check the region bound.}
Some more words here. \commentalice[id=c-1]{A comment by Alice.}
And \todobob{Bob says hi.} \donealice{Done (really).}
""", r"\presetkeys{todonotes}{inline,backgroundcolor=yellow,bordercolor=black,linecolor=cyan}{}" + PERSONS)
    notes = [e for e in pdf.annotations(path) if e["type"] == "Text"]
    assert [(e["author"], e["note"]) for e in notes] == [
        ("Alice", "Check the region bound."), ("Alice", "A comment by Alice."),
        ("Bob", "Bob says hi."), ("Alice", "Done (really)."),
    ]
    ids = by_id(path)
    assert ids["c-1"]["subject"] == "comment" and ids["c-1"]["nm"] == "pdfnotes:c-1"
    with pymupdf.open(path) as doc:
        subjects = [doc.xref_get_key(e["xref"], "Subj")[1] for e in notes]
        colours = [raw(doc, e["xref"], "C") for e in notes]
        nms = [raw(doc, e["xref"], "NM") for e in notes]
        assert info(doc, "PDFNotes") == "active"
        text = doc[0].get_text()
        # the Text annotation lies over the todonotes box (inline: the text's width)
        r = doc[0].first_annot
        rect = pymupdf.Rect([float(v) for v in raw(doc, r.xref, "Rect").strip("[]").split()])
        assert rect.width > 300 and 8 < rect.height < 20
    assert subjects == ["todo", "comment", "todo", "done"]
    # palette names give the palette's colours (pdf.COLORS)
    for c, name in zip(colours, ("pink", "cyan", "orange", "green")):
        assert close(rgb(c), pdf.COLORS[name])
    assert all(re.fullmatch(r"pdfnotes:[0-9a-f]{10}", n) for n in nms[:1] + nms[2:])
    assert len(set(nms)) == 4
    # todonotes' boxes are still there, prefix included (the prefix is not in /Contents)
    for s in ("Check the region bound.", "Alice: A comment by Alice.", "Bob: Bob says hi."):
        assert s in text.replace("\n", " ")


def test_shorthand_key_forms(tmp_path):
    """\\newpdfnote{\\cmd}{keys}, \\newpdfnote{\\cmd}[keys] and \\newpdfnote{\\cmd} alike."""
    path, _ = build(tmp_path, r"\braced{One.} \bracketed{Two.} \bare{Three.} \hl{marked}{Four.}", "\n".join([
        r"\newpdfnote{\braced}{kind=a,author=A}", r"\newpdfnote{\bracketed}[kind=b]", r"\newpdfnote{\bare}",
        r"\newpdfmark{\hl}[kind=d]{author=D,note=false}"]))
    got = {e["note"]: (e["author"], e["subject"]) for e in by_id(path).values()}
    assert got == {"One.": ("A", "a"), "Two.": ("", "b"), "Three.": ("", ""), "Four.": ("D", "d")}


def test_contents_round_trip(tmp_path):
    """Plain text written with TeX's special characters escaped comes back exactly."""
    escape = {"\\": r"\textbackslash{}", "%": r"\%", "&": r"\&", "#": r"\#", "_": r"\_",
              "{": r"\{", "}": r"\}", "$": r"\$", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
              "\n": r"\\"}
    notes = [
        r"50% & # _ {x} $ \ ~ ^ end",
        "line one\nline two\nline three",
        "Grüße — café, naïve, Ölçer",
        "a path C:\\tmp\\x and 100% (sure)",
        "tilde~caret^under_score",
    ]
    body = "\n".join(rf"\pdfnote[id=r{i},author={{Ünal, B.}},kind=to do]{{{''.join(escape.get(c, c) for c in s)}}}"
                     for i, s in enumerate(notes))
    body += "\n" + r"\pdfnote[id=x1]{\emph{x}   y \textbf{bold}}" + "\n" + r"\pdfnote[id=x2]{a\newline b \\ c}"
    path, _ = build(tmp_path, body)
    ids = by_id(path)
    for i, s in enumerate(notes):
        assert ids[f"r{i}"]["note"] == s
        assert ids[f"r{i}"]["author"] == "Ünal, B." and ids[f"r{i}"]["subject"] == "to do"
    assert ids["x1"]["note"] == "x y bold"
    assert ids["x2"]["note"] == "a\nb\nc"


def test_colours_hex_and_xcolor(tmp_path):
    path, _ = build(tmp_path, r"""
\pdfnote[id=h,color=#1a2B3c]{Hex colour.}
\pdfnote[id=n,color=red!50!blue]{An xcolor expression.}
\pdfmark[id=m,color=#FF8800,note=false]{marked}{Hex on a mark.}
""", r"\newpdfnote{\hexnote}{color=#00ff00,author=H}")
    ids = by_id(path)
    assert rgb(ids["h"]["C"]) == [0.102, 0.1686, 0.2353]   # 26/255, 43/255, 60/255 to 4 places
    assert rgb(ids["n"]["C"]) == [0.5, 0, 0.5]
    assert rgb(ids["m"]["C"]) == [1, 0.5333, 0]


def test_palette_names_match_pdf_colors(tmp_path):
    """color=<palette name> gives exactly the palette of this package (pdf.COLORS); xcolor's
    own colours of the same name stay as they are for the rest of the document."""
    body = "\n".join(rf"\pdfnote[id={n},color={n}]{{{n}}}" for n in pdf.COLORS)
    body += "\n" + r"\pdfnote[id=xc,color=pink!100]{xcolor's pink}"
    body += "\n" + r"\textcolor{blue}{BLUE}"
    path, _ = build(tmp_path, body)
    ids = by_id(path)
    for name, want in pdf.COLORS.items():
        assert close(rgb(ids[name]["C"]), want), name
    assert close(rgb(ids["xc"]["C"]), (1, 0.75, 0.75))
    with pymupdf.open(path) as doc:  # \textcolor{blue} is still xcolor's blue
        spans = [s for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]
                 if s["text"].strip() == "BLUE"]
        assert spans and spans[0]["color"] == 0x0000FF


@pytest.mark.parametrize("todonotes", [r"\usepackage{todonotes}", r"\usepackage[disable]{todonotes}"])
def test_spaces_around_notes(tmp_path, todonotes):
    """`word \\pdfnote{x} next' and `word\\pdfnote{x} next' read `word next', with one space."""
    path, _ = build(tmp_path, r"""
Ref word next and so on.\par
One word \pdfnote{x} next and so on.\par
Two word\pdfnote{y} next and so on.\par
Three word \pdfnote[box=false]{z} next and so on.\par
Four word \pdfnote{p} \pdfnote[replyto=p]{r} next and so on.\par
Five word \pdfnote{x}
next and so on.\par
""", todonotes=todonotes)
    with pymupdf.open(path) as doc:
        words = doc[0].get_text("words", sort=False)
    gaps = []
    for i, w in enumerate(words):
        if w[4] == "word" and words[i + 1][4] == "next":
            gaps.append(round(words[i + 1][0] - w[2], 2))
    assert len(gaps) == 6 and max(gaps) - min(gaps) < 0.1, gaps


def test_ids_explicit_auto_and_duplicates(tmp_path):
    a, _ = build(tmp_path, r"\pdfnote[author=A]{Same text.} \pdfnote[author=A]{Same text.} \pdfnote[id=x]{One.}"
                           r" \pdfnote[id=x]{Two.}", name="a")
    b, _ = build(tmp_path, r"\pdfnote[author=B]{Inserted before.} \pdfnote[author=A]{Same text.}", name="b")
    with pymupdf.open(a) as doc:
        nms_a = [raw(doc, x.xref, "NM") for x in doc[0].annots()]
    with pymupdf.open(b) as doc:
        nms_b = [raw(doc, x.xref, "NM") for x in doc[0].annots()]
    assert re.fullmatch(r"pdfnotes:[0-9a-f]{10}", nms_a[0])
    assert nms_a[1] == nms_a[0] + "-2"          # an exact duplicate gets a suffix
    assert nms_a[2:] == ["pdfnotes:x", "pdfnotes:x-2"]
    assert nms_b[1] == nms_a[0]                 # a note added before it does not change its id


def test_reply_threads(tmp_path):
    path, log = build(tmp_path, r"""
\pdfnote[id=p,author=Alice]{Parent.}
\pdfnote[id=c,replyto=p,author=Bob]{Child.}
\pdfnote[id=g,replyto=c,author=Alice,box=false]{Grandchild.}
\pdfnote[id=e,replyto=later,author=Bob]{Early.}
\pdfnote[id=later,author=Bob]{Later.}
\pdfnote[id=o,replyto=nope]{Orphan.}
Text with \pdfmark[id=m,note=false,author=Carol]{a mark}{On a mark.} and
\pdfnote[id=r,replyto=m,author=Dan,box=false]{Reply to the mark.}
""")
    ids = by_id(path)
    assert "reply_to" not in ids["p"]
    assert ids["c"]["reply_to"] == ids["p"]["xref"]
    assert ids["g"]["reply_to"] == ids["c"]["xref"]
    assert ids["r"]["reply_to"] == ids["m"]["xref"]
    for k in ("e", "o", "later"):
        assert "reply_to" not in ids[k]
    flat = " ".join(log.split())
    assert "replyto=later: no earlier note or mark has this id" in flat
    assert "replyto=nope: no earlier note or mark has this id" in flat
    with pymupdf.open(path) as doc:  # PyMuPDF sees the thread too
        assert doc.xref_get_key(ids["g"]["xref"], "IRT")[1] == f"{ids['c']['xref']} 0 R"


# ---- marks ------------------------------------------------------------------------------

def test_mark_styles_cover_their_words(tmp_path):
    path, _ = build(tmp_path, r"""
Here is \pdfmark[id=h,author=Carol,color=yellow,note=false]{a highlighted phrase}{Highlit.} and
\pdfmark[id=u,style=underline,author=Carol,color=blue,note=false]{an underlined phrase}{Underlined.} and
\pdfmark[id=s,style=squiggly,author=Carol,color=green,note=false]{a squiggly one}{Squiggled.} and
\pdfmark[id=x,style=strikeout,author=Carol,color=red,note=false,kind=delete]{struck words}{Struck.} here.
\pdfmark[id=t,author=Dan]{with its note}{The note in a todonotes box.} too.
""")
    ids = by_id(path)
    assert [ids[k]["note"] for k in "husxt"] == ["Highlit.", "Underlined.", "Squiggled.", "Struck.",
                                                  "The note in a todonotes box."]
    # note=true: the note is also in a todonotes box, without a Text annotation of its own
    assert not any(e["type"] == "Text" for e in pdf.annotations(path))
    with pymupdf.open(path) as doc:
        assert "The note in a todonotes box." in " ".join(doc[0].get_text().split())
    assert {k: ids[k]["type"] for k in "husx"} == {"h": "Highlight", "u": "Underline", "s": "Squiggly",
                                                     "x": "StrikeOut"}
    assert ids["h"]["text"] == "a highlighted phrase" and ids["u"]["text"] == "an underlined phrase"
    assert ids["s"]["text"] == "a squiggly one" and ids["x"]["text"] == "struck words"
    assert ids["x"]["subject"] == "delete" and ids["x"]["author"] == "Carol"
    assert close(rgb(ids["u"]["C"]), pdf.COLORS["blue"])
    with pymupdf.open(path) as doc:
        page = doc[0]
        annots = {raw(doc, a.xref, "NM"): a for a in page.annots()}
        words = page.get_text("words", sort=False)
        for nm, (first, last) in {"h": ("a", "phrase"), "u": ("an", "phrase"), "s": ("a", "one"),
                                  "x": ("struck", "words")}.items():
            got = words_in_quads(page, pdf.annot_quads(annots[f"pdfnotes:{nm}"]))
            assert [words[i][4] for i in got][0] == first and [words[i][4] for i in got][-1] == last


def test_mark_over_lines_columns_and_a_page_break(tmp_path):
    long = ("Nam dui ligula, fringilla a, euismod sodales, sollicitudin vel, wisi. Morbi auctor lorem non justo. "
            "Nam lacus libero, pretium at, lobortis vitae, ultricies et, tellus. Donec aliquet, tortor sed "
            "accumsan bibendum, erat ligula aliquet magna, vitae ornare odio metus a mi. Morbi ac orci et nisl "
            r"hendrerit mollis, with a {\bfseries braced group that is long enough to run over the end of a "
            r"line} and math $a + b$ and \emph{markup} until ENDWORD")
    path, _ = build(tmp_path, rf"""
Lorem ipsum dolor sit amet. \pdfmark[id=a,note=false,author=A]{{STARTA {long}}}{{Over lines.}} after.

\begin{{itemize}}\item An item where \pdfmark[id=b,note=false,author=B]{{STARTB a mark in a list item runs
over the end of the line and continues ENDB}}{{In a list.}} stops.\end{{itemize}}
\newpage\noindent\rule{{1pt}}{{\dimexpr\textheight-6\baselineskip\relax}}

Filler words to get near the bottom of the page. \pdfmark[id=c,note=false,author=C]{{STARTC {long} and
{long}}}{{Over a page break.}} done.
""")
    with pymupdf.open(path) as doc:
        pages = list(doc)
        marks: dict[str, list[tuple[int, list[pymupdf.Quad]]]] = {}
        for page in pages:
            for a in page.annots():
                marks.setdefault(raw(doc, a.xref, "NM"), []).append((page.number, pdf.annot_quads(a)))
        # one annotation per page; the page break splits c in two with the same /NM
        assert [p for p, _ in marks["pdfnotes:a"]] == [0] and [p for p, _ in marks["pdfnotes:b"]] == [0]
        assert [p for p, _ in marks["pdfnotes:c"]] == [1, 2]
        for nm, first, last, lines in (("a", "STARTA", "ENDWORD", 5), ("b", "STARTB", "ENDB", 2)):
            (_, quads), = marks[f"pdfnotes:{nm}"]
            assert len(quads) >= lines
            assert words_in_quads(pages[0], quads) == span(pages[0], first, last)
        (_, c1), (_, c2) = marks["pdfnotes:c"]
        w1 = pages[1].get_text("words", sort=False)  # the last word is the page number
        start = [w[4] for w in w1].index("STARTC")
        assert words_in_quads(pages[1], c1) == list(range(start, len(w1) - 1))
        first2 = pages[2].get_text("words", sort=False)[0][4]
        assert words_in_quads(pages[2], c2) == span(pages[2], first2, "ENDWORD")


def test_marks_and_notes_do_not_change_the_typesetting(tmp_path):
    body = r"""
Lorem ipsum dolor sit amet, \pdfmark[note=false]{consectetur adipiscing elit \label{l} with a label,
a citation \cite[p.~3]{x}, math $a + b = c$, \mbox{a box}, \index{i}an index entry and more words to wrap
over lines}{n} after. \pdfnote[box=false]{icon} More text follows here so that the paragraph is long
enough to have several lines, and \pdfmark[note=false,style=underline]{an underline at the end}{u}.
"""
    on, _ = build(tmp_path, body, name="on")
    off, _ = build(tmp_path, body, name="off", todonotes=r"\usepackage[disable]{todonotes}")
    with pymupdf.open(on) as a, pymupdf.open(off) as b:
        assert len(list(a[0].annots())) == 3 and not list(b[0].annots())
        assert a[0].get_text("words") == b[0].get_text("words")


# ---- disabled builds --------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["todonotes-disable", "option-disable", "option-final"])
def test_disabled_builds_have_no_notes_and_no_annotations(tmp_path, variant):
    todonotes = r"\usepackage[disable]{todonotes}" if variant == "todonotes-disable" else r"\usepackage{todonotes}"
    options = {"todonotes-disable": "", "option-disable": "disable", "option-final": "final"}[variant]
    if variant != "todonotes-disable":
        todonotes = ""  # pdfnotes loads todonotes itself and passes `disable' on
    path, _ = build(tmp_path, r"""
Before \todoalice{Hidden note.} after.
Some \flagalice{marked words}{hidden} here, \pdfmark[note=false]{more}{x} and \pdfnote[box=false]{y} end.
\pdfnote[id=p]{Parent.} \pdfnote[replyto=p]{Reply.} \todo{A plain todo.}
""", PERSONS, todonotes=todonotes, options=options)
    with pymupdf.open(path) as doc:
        assert not any(list(p.annots()) for p in doc)
        assert info(doc, "PDFNotes") is None
        text = " ".join(doc[0].get_text().split())
    assert "Before after. Some marked words here, more and end." in text
    assert "Hidden" not in text and "hidden" not in text and "todo" not in text


def test_info_key_pending_active_absent(tmp_path):
    """/PDFNotes in Info: pending on a run that could not place every note and mark (the first
    run with them), active once all are placed, absent when the notes are off."""
    body = r"Text \pdfmark[id=m]{marked words}{a note} and \pdfnote[id=n]{another}."
    first, _ = build(tmp_path, body, name="once", runs=1)
    with pymupdf.open(first) as doc:
        assert info(doc, "PDFNotes") == "pending"
        assert not any(list(p.annots()) for p in doc)
    second, _ = build(tmp_path, body, name="once", runs=1)  # the same document again
    with pymupdf.open(second) as doc:
        assert info(doc, "PDFNotes") == "active"
        assert {raw(doc, a.xref, "NM") for a in doc[0].annots()} == {"pdfnotes:m", "pdfnotes:n"}
    # a new note added to a finished document: pending again on its first run
    added, _ = build(tmp_path, body + r" \pdfnote[id=new]{new}", name="once", runs=1)
    with pymupdf.open(added) as doc:
        assert info(doc, "PDFNotes") == "pending"
    off, _ = build(tmp_path, body, name="off", todonotes=r"\usepackage[disable]{todonotes}")
    with pymupdf.open(off) as doc:
        assert info(doc, "PDFNotes") is None


def test_class_option_final_does_not_disable(tmp_path):
    path, _ = build(tmp_path, r"Before \pdfnote{Visible.} after.", classopts="final")
    with pymupdf.open(path) as doc:
        assert [a.info["content"] for a in doc[0].annots()] == ["Visible."]
        assert info(doc, "PDFNotes") == "active"


# ---- other engines ----------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["lualatex", "xelatex"])
def test_other_engines(tmp_path, engine):
    if not shutil.which(engine):
        pytest.skip(f"{engine} is not installed")
    path, _ = build(tmp_path, r"""
\pdfnote[id=p,author=Alice,kind=todo,color=pink]{A note, café.}
\pdfnote[id=c,replyto=p,author=Bob]{A reply.}
Lorem ipsum dolor sit amet, \pdfmark[id=m,note=false,author=Carol,style=underline]{STARTM consectetur
adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam,
quis nostrud exercitation ullamco laboris ENDM}{Underlined.} after.
""", engine=engine)
    ids = by_id(path)
    assert (ids["p"]["author"], ids["p"]["note"], ids["p"]["subject"]) == ("Alice", "A note, café.", "todo")
    assert close(rgb(ids["p"]["C"]), pdf.COLORS["pink"])
    assert ids["c"]["reply_to"] == ids["p"]["xref"]
    with pymupdf.open(path) as doc:
        assert info(doc, "PDFNotes") == "active"
        page = doc[0]
        annot = next(a for a in page.annots() if raw(doc, a.xref, "NM") == "pdfnotes:m")
        assert words_in_quads(page, pdf.annot_quads(annot)) == span(page, "STARTM", "ENDM")
