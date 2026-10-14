"""What the viewer servers hand over: a path and a 1-based page from ``viewer_state``,
and the selected text from ``get_selection``, fed to ``locate``, ``context`` and
``mark_text``.

Okular (poppler) gives a selection as the page's lines joined by newlines, a hyphen at
a line end kept, and the page's own characters (a ligature where the text layer has
one). The VS Code viewer (pdf.js) normalises it first (vscode-pdf-mcp,
``media/textnorm.mjs``, ported below as ``pdfjs``): lines joined by spaces, a line-end
hyphen between two lower-case letters dropped, and ligatures spelt out (pdf.js's
``normalizeUnicode``).
"""

import re
import shutil
import subprocess
import unicodedata

import pymupdf
import pytest

from pdf_reader_writer_mcp import pdf

needs_tex = pytest.mark.skipif(not shutil.which("pdflatex"), reason="needs pdflatex")


def pdfjs(raw: str) -> str:
    """``normaliseSelection`` of vscode-pdf-mcp's media/textnorm.mjs, applied to the
    text layer's ``Selection.toString()``, whose ligatures pdf.js already spelt out."""
    raw = unicodedata.normalize("NFKC", raw)
    raw = re.sub(r"\r\n?", "\n", raw)
    raw = re.sub(r"­[ \t]*\n\s*", "", raw)
    # /(\p{Ll})-[ \t]*\n\s*(?=\p{Ll})/gu -> "$1"
    raw = re.sub(r"([^\W\d_])-[ \t]*\n\s*(?=([^\W\d_]))",
                 lambda m: m.group(1) if m.group(1).islower() and m.group(2).islower() else m.group(), raw)
    raw = raw.replace("­", "")
    return re.sub(r"\s+", " ", raw).strip()


def test_pdfjs_port_matches_its_source():
    """The cases of vscode-pdf-mcp's textnorm tests."""
    assert pdfjs("dif-\nficult") == "difficult"
    assert pdfjs("first-\nClass") == "first- Class"  # not between lower-case letters: kept
    assert pdfjs("a\r\nb  c\n") == "a b c"
    assert pdfjs("soft­\nhyphen") == "softhyphen"
    assert pdfjs("speciﬁcation") == "specification"


HYPHENATED = ["The specification of the official workflow is dif-",
              "ficult to satisfy, and the configuration reflects first-",
              "class fluffy offices. Characteristically incompre-",
              "hensible considerations notwithstanding."]


@pytest.fixture
def hyphenated(tmp_path):
    """One paragraph broken at hyphens, then a second paragraph."""
    path = tmp_path / "h.pdf"
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "\n".join(HYPHENATED), fontsize=10)
    pg.insert_text((72, 200), "Second paragraph here.", fontsize=10)
    doc.save(path); doc.close()
    return str(path)


def okular(first, a, last, b):
    """Okular's selection from line ``first`` column ``a`` to line ``last`` column ``b``
    (1-based lines, Python slice columns), newline-joined as poppler gives it."""
    if first == last:
        return HYPHENATED[first - 1][a:b]
    return "\n".join([HYPHENATED[first - 1][a:]] + HYPHENATED[first:last - 1] + [HYPHENATED[last - 1][:b]])


# (first line, start column, last line, end column) of a drag over the paragraph
SELECTIONS = {
    "across a hyphenated break": (1, 4, 2, 17),
    "two hyphenated breaks": (1, 33, 3, 19),
    "a compound broken at its hyphen": (2, 46, 3, 12),
    "starting and ending mid-word": (1, 7, 2, 3),
    "ending at a line-end hyphen": (2, 0, 2, 57),
    "starting after a line-end hyphen": (2, 0, 2, 9),
    "the whole paragraph": (1, 0, 4, 41),
}


@pytest.mark.parametrize("viewer", ["okular", "pdfjs"])
@pytest.mark.parametrize("name", list(SELECTIONS))
def test_selection_locates_to_its_lines(hyphenated, viewer, name):
    first, a, last, b = SELECTIONS[name]
    raw = okular(first, a, last, b)
    text = raw if viewer == "okular" else pdfjs(raw)
    want = {"page": 1, "lines": f"{first}-{last}"}
    assert pdf.locate(hyphenated, 1, text) == want, text
    c = pdf.context(hyphenated, 1, text)
    assert c["lines"] == want["lines"] and c["paragraph_lines"] == "1-4"
    assert c["after"] == "Second paragraph here."


def test_pdfjs_selection_across_a_hyphen_used_to_be_lost(hyphenated):
    """Regression: MuPDF's search does not dehyphenate, so "difficult" (pdf.js) was not
    found; locate fell back to the first six words and reported line 1 only, or None."""
    assert pdf.locate(hyphenated, 1, "is difficult to satisfy") == {"page": 1, "lines": "1-2"}
    assert pdf.locate(hyphenated, 1, "Characteristically incomprehensible") == {"page": 1, "lines": "3-4"}
    assert pdf.locate(hyphenated, 1, "the official workflow is difficult to satisfy, and the configuration") \
        == {"page": 1, "lines": "1-2"}


@pytest.mark.parametrize("viewer", ["okular", "pdfjs"])
def test_mark_text_takes_the_selection_as_given(hyphenated, viewer):
    """The model marks what the user selected: one annotation over lines 1 and 2."""
    raw = okular(1, 34, 2, 17)  # "workflow is dif-\nficult to satisfy"
    text = raw if viewer == "okular" else pdfjs(raw)
    r = pdf.mark_text(hyphenated, 1, text, note="hm")
    (a,) = pdf.annotations(hyphenated)
    assert a["xref"] == r["xref"] and a["note"] == "hm"
    assert a["text"] == "workflow is dif- ficult to satisfy,"
    with pymupdf.open(hyphenated) as doc:
        pg = doc[0]
        ls = pdf._lines(pg)
        quads = pdf.annot_quads(next(pg.annots()))
        hit = {l["n"] for l in ls for q in quads if l["bbox"].intersects(q.rect)}
    assert hit == {1, 2}


def test_dehyphenated_text_is_marked_wherever_it_occurs(tmp_path):
    path = str(tmp_path / "d.pdf")
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 72), "it is dif-\nficult here", fontsize=10)
    pg.insert_text((72, 200), "and dif-\nficult there", fontsize=10)
    doc.save(path); doc.close()
    pdf.mark_text(path, 1, "difficult")
    (a,) = pdf.annotations(path)
    assert a["text"] == "dif- ficult dif- ficult"


def test_only_a_line_end_hyphen_is_optional_on_the_page(tmp_path):
    """The quote's hyphens are optional (one may be the line-end hyphen pdf.js drops),
    the page's are not, except at a line end."""
    path = str(tmp_path / "k.pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "a well-known fact", fontsize=10)
    doc.save(path); doc.close()
    for text in ("well-known", "well- known", "well-\nknown", "a well-known"):
        assert pdf.locate(path, 1, text) == {"page": 1, "lines": "1-1"}, text
    assert pdf.locate(path, 1, "wellknown") is None
    assert pdf.locate(path, 1, "awell-knownfact") == {"page": 1, "lines": "1-1"}  # spacing is not compared


@pytest.fixture
def ligatures(tmp_path):
    """A text layer with ligature characters (U+FB01, U+FB03), as many publishers' PDFs have."""
    path = tmp_path / "l.pdf"
    doc = pymupdf.open()
    pg = doc.new_page()
    tw = pymupdf.TextWriter(pg.rect)
    font = pymupdf.Font("tiro")
    tw.append((72, 72), "The speciﬁcation is oﬃcial.", font=font, fontsize=11)
    tw.append((72, 90), "A second line, no ligatures.", font=font, fontsize=11)
    tw.write_text(pg)
    doc.save(path); doc.close()
    return str(path)


def test_ligatures_on_the_page(ligatures):
    assert "speciﬁcation" in pdf.page_text(ligatures, 1)
    for text in ("speciﬁcation is oﬃcial", "specification is official", "SPECIFICATION"):
        assert pdf.locate(ligatures, 1, text) == {"page": 1, "lines": "1-1"}, text
    pdf.mark_text(ligatures, 1, "specification is official")
    (a,) = pdf.annotations(ligatures)
    assert a["text"] == "The speciﬁcation is oﬃcial."[4:]


def test_ligatures_in_the_selection_only(hyphenated):
    """Okular may hand over U+FB01 where the text layer spells "fi"."""
    assert pdf.locate(hyphenated, 1, "The speciﬁcation of the oﬃcial") == {"page": 1, "lines": "1-1"}
    assert pdf.locate(hyphenated, 1, "speci­fication") == {"page": 1, "lines": "1-1"}


TEX = r"""\documentclass{article}
\usepackage[paperwidth=4in,paperheight=6in,margin=0.5in]{geometry}
\begin{document}
The specification of the official workflow is difficult to satisfy, and the
configuration reflects first-class fluffy offices. Characteristically
incomprehensible internationalization considerations notwithstanding,
we continue with an extraordinarily straightforward demonstration.

Second paragraph here.
\end{document}
"""


@needs_tex
def test_tex_selections_from_both_viewers(tmp_path):
    """pdflatex hyphenates and sets ligatures; every line-spanning drag over the paragraph
    is found on its lines, as Okular and as the VS Code viewer hand it over."""
    (tmp_path / "doc.tex").write_text(TEX)
    subprocess.run(["pdflatex", "-interaction=nonstopmode", "doc.tex"], cwd=tmp_path,
                   capture_output=True, timeout=120, check=True)
    path = str(tmp_path / "doc.pdf")
    with pymupdf.open(path) as doc:
        lines = [l["text"] for l in pdf._lines(doc[0])]
    para = [i for i, l in enumerate(lines, 1) if not l.startswith(("Second", "1"))]
    assert any(l.endswith("-") for l in lines[:len(para) - 1])  # the test needs hyphenation
    for first in para:
        for last in para[first - 1:]:
            raw = "\n".join(lines[first - 1:last])[3:-3]  # mid-word at both ends
            for text in (raw, pdfjs(raw)):
                assert pdf.locate(path, 1, text) == {"page": 1, "lines": f"{first}-{last}"}, text
