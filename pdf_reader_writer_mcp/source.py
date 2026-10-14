"""Carry a PDF's annotations into its LaTeX sources as pdfnotes macros.

Marks in a PDF built from LaTeX vanish on rebuild. ``sync`` writes the PDF-only
highlights and notes into the sources as ``\\pdfmark``/``\\pdfnote`` macros of the
pdfnotes package, which re-emits them on the next build with /NM
``pdfnotes:<id>``, so a second sync finds them in place and does nothing.
SyncTeX maps each annotation to its source line.
"""

from __future__ import annotations

import bisect
import gzip
import itertools
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pymupdf

from . import pdf

PACKAGE = "pdfnotes"
NM_PREFIX = PACKAGE + ":"
WINDOW = 3  # source lines searched on either side of the SyncTeX line

# PDF text that the source spells differently.
_TYPESET = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
            "“": "``", "”": "''", "‘": "`", "’": "'", "–": "--", "—": "---"}
# /Subj values viewers write by themselves; anything else is a deliberate kind.
_DEFAULT_SUBJ = {"", "highlight", "underline", "squiggly", "strikeout", "strikethrough",
                 "text", "note", "comment", "sticky note"}
_WORD = re.compile(r"[A-Za-z]+")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


# --- LaTeX escaping ---

_ESC = {"\\": r"\textbackslash{}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "\n": "\\newline "}
_ESC.update({c: "\\" + c for c in "%&#_{}$"})
_UNESC = re.compile(r"\\(textbackslash|textasciitilde|textasciicircum|newline)(?![A-Za-z])(?:\{\}| ?)"
                    r"|\\\\|\\([%&#_{}$ ])|%[^\n]*\n?|~|\s*\n\s*")
_UNESC_WORDS = {"textbackslash": "\\", "textasciitilde": "~", "textasciicircum": "^", "newline": "\n"}


def escape(text: str) -> str:
    """Note text as LaTeX: specials escaped, newlines as ``\\newline``."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(_ESC.get(c, c) for c in text)


def unescape(src: str) -> str:
    """The inverse of ``escape``; ``\\\\`` is a newline too, a source line break a space."""
    def one(m: re.Match[str]) -> str:
        t = m.group()
        if m.group(1):
            return _UNESC_WORDS[m.group(1)]
        if m.group(2):
            return m.group(2)
        if t == "\\\\":
            return "\n"
        if t.startswith("%"):
            return ""
        return " "  # ~ or a line break in the source
    return _UNESC.sub(one, src)


def _norm(text: str) -> str:
    """Runs of spaces collapsed to one, as TeX does, for comparing notes."""
    return re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


# --- the macro scanner ---

@dataclass
class Opt:
    key: str
    value: str  # outer braces stripped
    span: tuple[int, int]  # the whole ``key=value`` item
    vspan: tuple[int, int] | None  # the raw value, braces included; None for a bare key


@dataclass
class Macro:
    name: str  # as written: pdfnote, pdfmark or a shorthand
    kind: str  # "note" or "mark"
    start: int
    end: int
    head: int  # just after the macro name
    opts: list[Opt]
    bracket: tuple[int, int] | None  # ``[...]``, brackets included
    args: list[tuple[int, int]]  # inside the braces

    def opt(self, key: str) -> str | None:
        return next((o.value for o in self.opts if o.key == key), None)

    @property
    def id(self) -> str | None:
        return self.opt("id") or None


def _skip_comment(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j + 1


def _strip_comments(text: str) -> str:
    return re.sub(r"((?:^|[^\\])(?:\\\\)*)%.*", r"\1", text, flags=re.M)


def _close(text: str, i: int, close: str) -> int:
    """Index just past the group opened at ``text[i]`` (``{`` or ``[``), -1 if unbalanced."""
    depth, i = 0, i + 1
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            i = _skip_comment(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return i + 1 if close == "}" else -1
            depth -= 1
        elif c == close and depth == 0:
            return i + 1
        i += 1
    return -1


def _skip_space(text: str, i: int) -> int:
    """Past spaces and at most one line break: a blank line ends the macro."""
    nl = False
    while i < len(text) and text[i] in " \t\r\n":
        if text[i] == "\n":
            if nl:
                break
            nl = True
        i += 1
    return i


def _opt(text: str, s: int, e: int, out: list[Opt]) -> None:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    if s == e:
        return
    k = text.find("=", s, e)
    if k < 0:
        out.append(Opt(text[s:e], "", (s, e), None))
        return
    vs, ve = k + 1, e
    while vs < ve and text[vs].isspace():
        vs += 1
    raw = text[vs:ve]
    value = raw[1:-1] if raw.startswith("{") and _close(raw, 0, "}") == len(raw) else raw
    out.append(Opt(text[s:k].strip(), value, (s, e), (vs, ve)))


def _opts(text: str, a: int, b: int) -> list[Opt]:
    """The ``key=value`` items of ``text[a:b]``, split at commas outside braces."""
    out: list[Opt] = []
    depth, i, item = 0, a, a
    while i < b:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "," and depth == 0:
            _opt(text, item, i, out)
            item = i + 1
        i += 1
    _opt(text, item, b, out)
    return out


def _macro(text: str, name: str, kind: str, start: int, head: int) -> Macro | None:
    j = _skip_space(text, head)
    bracket, opts = None, []
    if j < len(text) and text[j] == "[":
        k = _close(text, j, "]")
        if k < 0:
            return None
        bracket, opts = (j, k), _opts(text, j + 1, k - 1)
    end = bracket[1] if bracket else head
    args = []
    for _ in range(2 if kind == "mark" else 1):
        j = _skip_space(text, end)
        if j >= len(text) or text[j] != "{":
            return None
        end = _close(text, j, "}")
        if end < 0:
            return None
        args.append((j + 1, end - 1))
    return Macro(name, kind, start, end, head, opts, bracket, args)


def aliases(text: str) -> dict[str, str]:
    """Shorthands declared by ``\\newpdfnote{\\name}`` / ``\\newpdfmark{\\name}``: name -> kind."""
    return {m.group(2): m.group(1)
            for m in re.finditer(r"\\newpdf(note|mark)\s*\{?\s*\\([A-Za-z]+)", _strip_comments(text))}


def scan(text: str, names: dict[str, str]) -> list[Macro]:
    """Every call of the macros in ``names`` (name -> "note"/"mark") outside comments,
    nested ones included, in source order."""
    out: list[Macro] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "%":
            i = _skip_comment(text, i)
            continue
        if c != "\\":
            i += 1
            continue
        m = _WORD.match(text, i + 1)
        if not m:
            i += 2
            continue
        i = m.end()
        declared = re.search(r"\\newpdf(?:note|mark)\s*\{?\s*$", text[max(0, m.start() - 40):m.start() - 1])
        if m.group() in names and not declared:  # not the shorthand's own declaration
            mac = _macro(text, m.group(), names[m.group()], m.start() - 1, i)
            if mac:
                out.append(mac)
    return out


# --- source files ---

class Source:
    """A .tex file as text with "\\n" line ends, and the edits planned on it."""

    def __init__(self, path: str, text: str | None = None) -> None:
        self.path = path
        if text is None:
            with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
                text = f.read()
        raw = text
        self.crlf = "\r\n" in raw and raw.count("\r\n") == raw.count("\n")
        self.text = raw.replace("\r\n", "\n") if self.crlf else raw
        self.starts = [0] + [m.end() for m in re.finditer("\n", self.text)]
        self.macros: list[Macro] = []
        self.edits: list[tuple[int, int, str, int]] = []
        self.wraps: list[tuple[int, int]] = []  # spans wrapped in a new \pdfmark
        self.points: list[int] = []  # where new macros go

    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)

    def bounds(self, line: int) -> tuple[int, int]:
        """Start and end (before the newline) of 1-based ``line``."""
        s = self.starts[line - 1]
        e = self.starts[line] - 1 if line < len(self.starts) else len(self.text)
        return s, e

    def result(self) -> str:
        """The text with the edits applied; an edit overlapping an earlier one is dropped.
        Insertions at one position keep their order and go before a replacement there."""
        out, pos = [], 0
        for s, e, t, _ in sorted(self.edits, key=lambda x: (x[0], x[1] > x[0], x[3])):
            if s < pos:
                continue
            out += [self.text[pos:s], t]
            pos = e
        out.append(self.text[pos:])
        return "".join(out)


def _write(path: str, text: str, crlf: bool) -> None:
    """Replace ``path`` atomically, keeping its mode bits; a symlink stays one and its
    target is replaced."""
    path = os.path.realpath(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text.replace("\n", "\r\n") if crlf else text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --- finding the marked text ---

def _visible(text: str, a: int, b: int, hidden: list[tuple[int, int]]) -> tuple[str, list[int], list[int]]:
    """Roughly what ``text[a:b]`` typesets to, with each character's source span.

    Control words, braces and ``$`` vanish, whitespace collapses to one space, and
    a blank line, a comment or ``\\par`` become a NUL barrier that no match
    crosses. ``hidden`` spans (existing macros) are skipped."""
    jump = dict(hidden)
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []

    def emit(ch: str, s: int, e: int) -> None:
        if ch == " " and (not chars or chars[-1] in " \0"):
            return
        chars.append(ch)
        starts.append(s)
        ends.append(e)

    i = max([a] + [e for s, e in hidden if s < a < e])
    while i < b:
        if i in jump:
            i = jump[i]
            continue
        c = text[i]
        if c == "%":
            emit("\0", i, i)
            i = _skip_comment(text, i)
        elif c == "\\":
            m = _WORD.match(text, i + 1)
            if m:
                if m.group() == "par":
                    emit("\0", i, m.end())
                i = m.end()
                while i < b and text[i] in " \t":
                    i += 1
                continue
            nxt = text[i + 1:i + 2]
            if nxt and nxt in "%&#_{}$":
                emit(nxt, i, i + 2)
            elif nxt and nxt in "\\ \n~":
                emit(" ", i, i + 2)
            i += 2  # other control symbols (\- \, \/) typeset nothing
        elif c in "{}$":
            i += 1
        elif c == "~":
            emit(" ", i, i + 1)
            i += 1
        elif c in " \t\r\n":
            j, nl = i, 0
            while j < b and text[j] in " \t\r\n":
                nl += text[j] == "\n"
                j += 1
            emit("\0" if nl > 1 else " ", i, j)
            i = j
        else:
            emit(c, i, i + 1)
            i += 1
    return "".join(chars), starts, ends


def _pattern(needle: str) -> re.Pattern[str] | None:
    """The PDF's marked text as a pattern over ``_visible`` text: ligatures and
    typeset quotes and dashes spelt as in the source, and a word broken across
    lines (``exam- ple``) matching ``example`` or ``exam-ple``."""
    s = " ".join("".join(_TYPESET.get(c, c) for c in needle).split())
    if not s:
        return None
    pieces = re.split(r"(?<=[^\W\d_])- (?=[^\W\d_])", s)
    return re.compile("-?".join(re.escape(p) for p in pieces))


def _balanced(text: str, s: int, e: int) -> bool:
    depth, dollars, i = 0, 0, s
    while i < e:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        elif c == "$":
            dollars += 1
        i += 1
    return depth == 0 and dollars % 2 == 0


def _overlaps(s: int, e: int, bs: int, be: int) -> bool:
    return s < be and bs < e if be > bs else s < bs < e


def find_text(src: Source, needle: str, line: int) -> tuple[int, int] | None:
    """The source span of ``needle`` closest to ``line``, searched within
    ``WINDOW`` lines of it; it may not cross a blank line, a comment, unbalanced
    braces or a macro (existing or planned)."""
    pat = _pattern(needle)
    if pat is None:
        return None
    lo = max(1, line - WINDOW)
    hi = min(len(src.starts), line + WINDOW + len(needle) // 40)
    a, b = src.starts[lo - 1], src.bounds(hi)[1]
    hidden = []
    for m in src.macros:
        if m.kind == "note":
            hidden.append((m.start, m.end))
        else:
            hidden += [(m.start, m.args[0][0]), (m.args[0][1], m.end)]
    vis, starts, ends = _visible(src.text, a, b, hidden)
    blocked = [(m.start, m.end) for m in src.macros] + src.wraps + [(p, p) for p in src.points]
    best: tuple[int, int, int] | None = None
    for m in pat.finditer(vis):
        s, e = starts[m.start()], ends[m.end() - 1]
        if not _balanced(src.text, s, e) or any(_overlaps(s, e, bs, be) for bs, be in blocked):
            continue
        d = abs(src.line_of(s) - line)
        if best is None or d < best[0]:
            best = (d, s, e)
    return best[1:] if best else None


def note_point(src: Source, line: int) -> int:
    """Where a new ``\\pdfnote`` goes for ``line``: its end, before an unescaped
    ``%`` and trailing blanks; the previous non-blank line if it is blank; past a
    macro or new mark that would otherwise enclose it."""
    while True:
        s, e = src.bounds(line)
        i = s
        while i < e and src.text[i] != "%":
            i += 2 if src.text[i] == "\\" else 1
        p = min(i, e)
        while p > s and src.text[p - 1] in " \t\r":
            p -= 1
        if p > s or line == 1:
            break
        line -= 1
    spans = [(m.start, m.end) for m in src.macros] + src.wraps
    moved = True
    while moved:
        moved = False
        for ms, me in spans:
            if ms < p < me:
                p, moved = me, True
    return p


# --- SyncTeX ---

def synctex_file(path: str) -> str | None:
    stem = os.path.splitext(path)[0]
    return next((f for f in (stem + ".synctex.gz", stem + ".synctex") if os.path.isfile(f)), None)


def synctex_inputs(stx: str) -> list[str]:
    """The ``Input:`` files of a SyncTeX file, the main file first, as absolute paths."""
    opener = gzip.open if stx.endswith(".gz") else open
    base = os.path.dirname(stx)
    out: list[str] = []
    with opener(stx, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("Input:"):
                p = os.path.normpath(os.path.join(base, line.rstrip("\n").split(":", 2)[2]))
                if p not in out:
                    out.append(p)
    return out


def synctex_edit(path: str, page: int, x: float, y: float) -> list[tuple[str, int]]:
    """Source (file, line) pairs for a point, in big points from the page's top left."""
    exe = shutil.which("synctex")
    if exe is None:
        raise RuntimeError("synctex not found on PATH (it comes with TeX Live)")
    base = os.path.dirname(path)
    out = subprocess.run([exe, "edit", "-o", f"{page}:{x:.2f}:{y:.2f}:{path}"], cwd=base,
                         capture_output=True, text=True, timeout=30).stdout
    hits, cur = [], None
    for line in out.splitlines():
        k, _, v = line.partition(":")
        if k == "Input":
            cur = os.path.normpath(os.path.join(base, v))
        elif k == "Line" and cur and v.strip().isdigit():
            hits.append((cur, int(v)))
    return hits


def _probe(pg: pymupdf.Page, annot: pymupdf.Annot, markup: bool) -> pymupdf.Point:
    """The point SyncTeX is asked about: the centre of a markup's first quad, or of a
    note's icon moved onto the nearest word level with it (the margin maps to the
    paragraph's end, not to the line)."""
    quads = pdf.annot_quads(annot) if markup else []
    r = quads[0].rect if quads else annot.rect
    p = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
    if not markup:
        near = min(((max(w[1] - p.y, p.y - w[3], 0), abs((w[0] + w[2]) / 2 - p.x), w)
                    for w in pg.get_text("words")), default=None)
        if near and near[0] <= 12:
            w = near[2]
            p = pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
    return p + pg.cropbox_position  # SyncTeX counts from the media box


def marked_text(pg: pymupdf.Page, annot: pymupdf.Annot) -> str:
    """The characters under a markup's quads, lines joined by a space. Tighter than
    ``pdf._marked_text``, which takes whole words and so their punctuation."""
    rects = [q.rect for q in pdf.annot_quads(annot)]
    lines = []
    for b in pg.get_text("rawdict")["blocks"]:
        for ln in b.get("lines", []):
            t = "".join(c["c"] for sp in ln["spans"] for c in sp["chars"]
                        if any(pymupdf.Point((c["bbox"][0] + c["bbox"][2]) / 2, (c["bbox"][1] + c["bbox"][3]) / 2) in r
                               for r in rects)).strip()
            if t:
                lines.append(t)
    return " ".join(lines) or pdf._marked_text(pg, annot)


# --- the sync ---

def loads_package(text: str) -> bool:
    for m in re.finditer(r"\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", _strip_comments(text)):
        if any(re.fullmatch(r"(?:.*/)?" + PACKAGE + r"(?:\.sty)?", p.strip()) for p in m.group(1).split(",")):
            return True
    return False


# --- deletions recorded in the PDF ---

DELETED_KEY = "PDFNotesDeleted"  # in the catalog: ids deleted through our tools, not yet synced
_PDF_STRING = re.compile(r"\((?:\\[0-7]{1,3}|\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>", re.S)


_PDF_ESC = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}


def _pdf_text(token: str) -> str:
    """A PDF string token, literal ``(...)`` or hex ``<...>``, as text."""
    if token.startswith("<"):
        b = bytes.fromhex("".join(token[1:-1].split()))
        return b[2:].decode("utf-16-be") if b[:2] == b"\xfe\xff" else b.decode("latin-1")

    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        return chr(int(e, 8)) if e[0].isdigit() else _PDF_ESC.get(e, e)
    return re.sub(r"\\([0-7]{1,3}|.)", one, token[1:-1], flags=re.S)


def deleted_ids(doc: pymupdf.Document) -> list[str]:
    """The pdfnotes ids deleted through ``delete_thread`` and not yet synced."""
    kind, value = doc.xref_get_key(doc.pdf_catalog(), DELETED_KEY)
    return [_pdf_text(t.group()) for t in _PDF_STRING.finditer(value)] if kind == "array" else []


def _store_deleted(doc: pymupdf.Document, ids: list[str]) -> None:
    value = "[" + " ".join(pymupdf.get_pdf_str(i) for i in ids) + "]" if ids else "null"
    doc.xref_set_key(doc.pdf_catalog(), DELETED_KEY, value)


def record_deleted(doc: pymupdf.Document, ids: list[str]) -> None:
    """Add ``ids`` to the catalog's list of deleted pdfnotes ids, saved with the deletion."""
    known = deleted_ids(doc)
    new = [i for i in dict.fromkeys(ids) if i not in known]
    if new:
        _store_deleted(doc, known + new)


def clear_deleted(doc: pymupdf.Document, ids: list[str] | set[str]) -> None:
    known = deleted_ids(doc)
    left = [i for i in known if i not in ids]
    if left != known:
        _store_deleted(doc, left)


def nm_id(doc: pymupdf.Document, xref: int) -> str:
    """The pdfnotes id in an annotation's /NM, or ""."""
    kind, nm = doc.xref_get_key(xref, "NM")
    return nm[len(NM_PREFIX):] if kind == "string" and nm.startswith(NM_PREFIX) else ""


def _hex(rgb: tuple[float, ...]) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def _value(v: str) -> str:
    return "{" + v + "}" if not v or v != v.strip() or re.search(r"[,=\[\]{}]", v) else v


def _same_color(spec: str, rgb: tuple[float, ...]) -> bool:
    """Whether a source colour matches /C within 1/255; True for colours we cannot read
    (xcolor names outside the palette, expressions), which are left alone."""
    spec = spec.strip()
    if not (_HEX.fullmatch(spec) or spec.lower() in pdf.COLORS):
        return True
    src = pdf.parse_color(spec, pdf.DEFAULT_COLOR)
    return all(abs(a - b) <= 1 / 255 + 1e-6 for a, b in zip(src, rgb))


@dataclass
class _Rec:
    """What ``sync`` needs of one markup or Text annotation."""
    xref: int
    page: int
    subtype: str
    markup: bool
    nm: str
    irt: int
    note: str
    author: str
    rgb: tuple[float, ...] | None
    kind: str
    probe: pymupdf.Point
    marked: str = ""


Probe = tuple[int, float, float]  # page, x, y (rounded) of a SyncTeX lookup


class _Sync:
    """One pass over the PDF and its sources. With ``plan`` it changes nothing and only
    collects the SyncTeX lookups it would need in ``misses``; ``cache`` holds the answers."""

    def __init__(self, path: str, doc: pymupdf.Document, stx: str,
                 cache: dict[Probe, list[tuple[str, int]]], plan: bool = False) -> None:
        self.path, self.doc, self.cache, self.plan = path, doc, cache, plan
        self.misses: set[Probe] = set()
        inputs = synctex_inputs(stx)
        if not inputs:
            raise ValueError(f"{stx} names no input files")
        self.main = inputs[0]
        self.root = os.path.dirname(self.main)
        self.srcs = {p: Source(p) for p in inputs
                     if p.endswith(".tex") and os.path.isfile(p)
                     and os.path.commonpath([self.root, p]) == self.root}
        if self.main not in self.srcs or not any(loads_package(s.text) for s in self.srcs.values()):
            raise ValueError(f"{self.main} does not load the {PACKAGE} package: "
                             f"add \\usepackage{{{PACKAGE}}} to its preamble")
        names = {"pdfnote": "note", "pdfmark": "mark"}
        for s in self.srcs.values():
            names.update(aliases(s.text))
        self.by_id: dict[str, tuple[Source, Macro]] = {}
        for s in self.srcs.values():
            s.macros = scan(s.text, names)
            for m in s.macros:
                if m.id and m.id not in self.by_id:
                    self.by_id[m.id] = (s, m)
        self.ids = set(self.by_id)
        self.synced: dict[int, str] = {}  # annotation xref -> id
        self.after: dict[str, tuple[Source, int]] = {}  # id -> where its replies go
        self.adopted: set[int] = set()  # id() of macros given an id this run
        self.seq = itertools.count()
        self.report: dict[str, Any] = {"source": self._rel(self.main), "inserted": [], "updated": [],
                                       "removed": [], "fallback": [], "skipped": []}

    def _rel(self, p: str) -> str:
        return os.path.relpath(p, self.root)

    def _edit(self, src: Source, s: int, e: int, text: str) -> None:
        src.edits.append((s, e, text, next(self.seq)))

    def _fresh(self) -> str:
        if self.plan:
            return "plan"  # never written; keeps the ids for the real pass
        while True:
            i = secrets.token_hex(4)
            if i not in self.ids:
                self.ids.add(i)
                return i

    def _skip(self, r: _Rec, reason: str) -> None:
        self.report["skipped"].append({"xref": r.xref, "page": r.page, "subtype": r.subtype, "reason": reason})

    def records(self) -> list[_Rec]:
        """The markup and Text annotations, parents before their replies; the rest
        reported as skipped (Popup, Link and Widget silently)."""
        recs: dict[int, _Rec] = {}
        for n in range(1, self.doc.page_count + 1):
            pg = self.doc[n - 1]
            for a in pg.annots():
                kind, subtype = a.type
                markup = kind in pdf._MARKUP
                if not markup and kind != pymupdf.PDF_ANNOT_TEXT:
                    if kind not in (pymupdf.PDF_ANNOT_POPUP, pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET):
                        self.report["skipped"].append({"xref": a.xref, "page": n, "subtype": subtype,
                                                       "reason": f"{subtype} annotations have no {PACKAGE} macro"})
                    continue
                info = a.info
                subj = info.get("subject", "")
                stroke = a.colors.get("stroke")
                recs[a.xref] = _Rec(
                    a.xref, n, subtype, markup, info.get("id", ""), 0 if markup else (a.irt_xref or 0),
                    info.get("content", "").replace("\r\n", "\n").replace("\r", "\n"),
                    info.get("title", ""), tuple(stroke) if stroke else None,
                    "" if subj.strip().lower() in _DEFAULT_SUBJ | {subtype.lower()} else subj,
                    _probe(pg, a, markup), marked_text(pg, a) if markup else "")
        order: list[_Rec] = []
        seen: set[int] = set()

        def visit(r: _Rec) -> None:
            if r.xref in seen:
                return
            seen.add(r.xref)
            if r.irt in recs:
                visit(recs[r.irt])
            order.append(r)
        for r in recs.values():
            visit(r)
        return order

    def run(self) -> dict[str, Any]:
        recs = self.records()
        pdf_ids = {a.info.get("id", "")[len(NM_PREFIX):] for pg in self.doc for a in pg.annots()
                   if a.info.get("id", "").startswith(NM_PREFIX)}
        recorded = deleted_ids(self.doc)
        self.ids |= pdf_ids | set(recorded)
        done: set[str] = set()
        for r in recs:
            nid = r.nm[len(NM_PREFIX):] if r.nm.startswith(NM_PREFIX) else ""
            if nid and nid in done:
                self.synced[r.xref] = nid  # one macro emitted as several annots (a page break)
                continue
            if r.irt and r.irt not in self.synced:
                self._skip(r, f"its parent (xref {r.irt}) is not in the sources")
                continue
            if nid in self.by_id:
                self.update(r, *self.by_id[nid])
            elif nid:
                self.adopt(r, nid)
            else:
                self.insert(r, self._fresh(), new=True)
            if r.xref in self.synced:
                done.add(self.synced[r.xref])
        if self.plan:
            return self.report
        # Only deletions made through our tools propagate: an id merely absent from the PDF
        # may come from a build that read the sources before the sync that added it.
        self.delete({i for i in recorded if i not in pdf_ids})
        clear_deleted(self.doc, recorded)
        pdf.check_unchanged(self.doc)  # before the sources: their ids are saved with the PDF
        for src in self.srcs.values():
            if src.edits:
                new = src.result()
                if new != src.text:
                    _write(src.path, new, src.crlf)
        return self.report

    def _locate(self, r: _Rec) -> list[tuple[Source, int]]:
        key = (r.page, round(r.probe.x, 1), round(r.probe.y, 1))
        if key not in self.cache:
            if self.plan:
                self.misses.add(key)
                return []
            self.cache[key] = synctex_edit(self.path, *key)  # added or moved since the plan
        hits = self.cache[key]
        return [(self.srcs[f], n) for f, n in hits if f in self.srcs]

    def _opts(self, r: _Rec, nid: str, style: bool) -> list[tuple[str, str]]:
        o = [("id", nid)]
        if r.author:
            o.append(("author", escape(r.author)))
        if r.rgb:
            o.append(("color", _hex(r.rgb)))
        st = pdf.style_of(r.subtype)
        if style and st and st != "highlight":
            o.append(("style", st))
        if r.kind:
            o.append(("kind", escape(r.kind)))
        if r.irt:
            o.append(("replyto", self.synced[r.irt]))
        return o

    @staticmethod
    def _call(name: str, opts: list[tuple[str, str]], *args: str) -> str:
        o = ",".join(f"{k}={_value(v)}" for k, v in opts)
        return f"\\{name}[{o}]" + "".join("{" + a + "}" for a in args)

    def _fallback_text(self, r: _Rec) -> str:
        return f"“{r.marked}”: {r.note}" if r.note else f"“{r.marked}”"

    def _item(self, nid: str, src: Source, pos: int, macro: str) -> dict[str, Any]:
        return {"id": nid, "file": self._rel(src.path), "line": src.line_of(pos), "macro": macro}

    def insert(self, r: _Rec, nid: str, new: bool) -> None:
        """Write ``r`` into the sources as a new macro with id ``nid``; ``new`` also
        stamps the annotation's /NM."""
        if r.irt:
            src, p = self.after[self.synced[r.irt]]
            self._edit(src, p, p, " " + self._call("pdfnote", self._opts(r, nid, False), escape(r.note)))
            src.points.append(p)
            self.report["inserted"].append(self._item(nid, src, p, "pdfnote"))
        else:
            hits = self._locate(r)
            if not hits:
                self._skip(r, "SyncTeX maps it to no editable source line")
                return
            src, line = hits[0]
            span = find_text(src, r.marked, line) if r.markup else None
            if span:
                s, p = span
                self._edit(src, s, s, self._call("pdfmark", self._opts(r, nid, True)) + "{")
                self._edit(src, p, p, "}{" + escape(r.note) + "}")
                src.wraps.append(span)
                self.report["inserted"].append(self._item(nid, src, s, "pdfmark"))
            else:
                p = note_point(src, line)
                text = self._fallback_text(r) if r.markup else r.note
                self._edit(src, p, p, " " + self._call("pdfnote", self._opts(r, nid, False), escape(text)))
                src.points.append(p)
                if r.markup:
                    self.report["fallback"].append({**self._item(nid, src, p, "pdfnote"), "text": r.marked})
                else:
                    self.report["inserted"].append(self._item(nid, src, p, "pdfnote"))
        self.after[nid] = (src, p)
        self.synced[r.xref] = nid
        if new:
            self.doc.xref_set_key(r.xref, "NM", pymupdf.get_pdf_str(NM_PREFIX + nid))

    def adopt(self, r: _Rec, nid: str) -> None:
        """An annotation with a pdfnotes id the sources do not spell out: a macro of the
        same kind without ``id=`` on its SyncTeX lines takes the id, else it is inserted."""
        kind = "mark" if r.markup else "note"
        best: tuple[int, Source, Macro] | None = None
        for src, line in self._locate(r) if not r.irt else []:
            for m in src.macros:
                if m.kind == kind and not m.id and id(m) not in self.adopted:
                    a, b = src.line_of(m.start), src.line_of(m.end)
                    d = 0 if a <= line <= b else min(abs(a - line), abs(b - line))
                    if d <= 1 and (best is None or d < best[0]):
                        best = (d, src, m)
        if best is None:
            self.insert(r, nid, new=False)
            return
        _, src, m = best
        self.adopted.add(id(m))
        self.update(r, src, m, {"id": nid})

    def update(self, r: _Rec, src: Source, m: Macro, extra: dict[str, str] | None = None) -> None:
        """Bring macro ``m`` in line with ``r``: note, author, style and colour, only
        what differs, other keys and their order kept."""
        nid = (extra or {}).get("id") or m.id or ""
        self.synced[r.xref] = nid
        self.after[nid] = (src, m.end)
        changes = dict(extra or {})
        changed = list(changes)
        a, b = m.args[0 if m.kind == "note" else 1]
        note = self._fallback_text(r) if m.kind == "note" and r.markup else r.note
        if _norm(unescape(src.text[a:b])) != _norm(note):
            self._edit(src, a, b, escape(note))
            changed.append("note")
        author = m.opt("author")
        if author is not None and _norm(unescape(author)) != _norm(r.author):
            changes["author"] = escape(r.author)
        style = pdf.style_of(r.subtype)
        if m.kind == "mark" and style and (m.opt("style") or "highlight").strip().lower() != style:
            changes["style"] = style
        color = m.opt("color")
        if color and r.rgb and not _same_color(color, r.rgb):
            changes["color"] = _hex(r.rgb)
        self._set_opts(src, m, changes)
        changed += [k for k in changes if k not in changed]
        if changed:
            self.report["updated"].append({"id": nid, "file": self._rel(src.path), "line": src.line_of(m.start),
                                           "macro": m.name, "changed": changed})

    def _set_opts(self, src: Source, m: Macro, changes: dict[str, str]) -> None:
        new = []
        for k, v in changes.items():
            o = next((o for o in m.opts if o.key == k), None)
            if o and o.vspan:
                self._edit(src, *o.vspan, _value(v))
            elif o:
                self._edit(src, *o.span, f"{k}={_value(v)}")
            else:
                new.append(f"{k}={_value(v)}")
        if not new:
            return
        if m.bracket is None:
            self._edit(src, m.head, m.head, "[" + ",".join(new) + "]")
        else:
            p = m.bracket[0] + 1
            self._edit(src, p, p, ",".join(new) + ("," if m.opts else ""))

    def delete(self, gone: set[str]) -> None:
        """Remove the macros of the deleted ids and their replies: a ``\\pdfnote`` with the
        space before it, a ``\\pdfmark`` unwrapped to its marked text."""
        macros = [(src, m) for src in self.srcs.values() for m in src.macros]
        dead: set[int] = set()
        grew = True
        while grew:
            grew = False
            for src, m in macros:
                if id(m) not in dead and (m.id in gone or (m.opt("replyto") or "") in gone):
                    dead.add(id(m))
                    if m.id:
                        gone = gone | {m.id}
                    grew = True
        for src, m in macros:
            if id(m) not in dead:
                continue
            if m.kind == "note":
                s = m.start - 1 if src.text[m.start - 1:m.start] == " " else m.start
                self._edit(src, s, m.end, "")
            else:
                self._edit(src, m.start, m.args[0][0], "")
                self._edit(src, m.args[0][1], m.end, "")
            self.report["removed"].append({"id": m.id or "", "file": self._rel(src.path),
                                           "line": src.line_of(m.start), "macro": m.name})


def sync(path: str) -> dict[str, Any]:
    """Write the PDF's highlights and notes into its LaTeX sources as pdfnotes macros,
    bring changed ones up to date and drop the ones deleted through ``delete_thread``
    (recorded in the catalog). The annotations written get /NM ``pdfnotes:<id>``,
    saved incrementally.

    The SyncTeX lookups are slow (one process each), so they are planned under a
    shared lock and run in parallel with no lock held. The sync itself then runs
    from fresh state under the exclusive lock, with only the lookups for marks
    added or moved in between left to do there."""
    path = os.path.abspath(path)
    real = os.path.realpath(path)
    if real != path and synctex_file(real):
        path = real  # a link to the build's PDF: its SyncTeX file is next to the target
    with pdf._open(path) as doc:
        stx = synctex_file(path)
        if stx is None:
            return {"source": None, "reason": "no SyncTeX file next to the PDF (build with -synctex=1)"}
        plan = _Sync(path, doc, stx, {}, plan=True)
        plan.run()
    cache = lookup(path, plan.misses)
    with pdf._open(path, write=True) as doc:
        stx = synctex_file(path) or stx
        return _Sync(path, doc, stx, cache).run()


def lookup(path: str, probes: set[Probe]) -> dict[Probe, list[tuple[str, int]]]:
    """``synctex_edit`` for each probe, in parallel; the CLI answers one ``-o`` per call."""
    keys = sorted(probes)
    if not keys:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1, len(keys))) as ex:
        return dict(zip(keys, ex.map(lambda k: synctex_edit(path, *k), keys)))
