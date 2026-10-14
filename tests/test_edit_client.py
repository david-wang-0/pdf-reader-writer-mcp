"""The edit service as vscode-pdf-mcp drives it: one long-lived process per window
(extension/editservice.js), requests from the mark controller and its undo stack
(extension/marks.js), and a ``sync`` after each burst of marks (extension/sourcesync.js).

Every test talks to a real ``pdf-reader-writer-edit`` process over pipes, waiting for
each answer with a deadline, as the client does: an answer that is not flushed, out of
order, or not on stdout fails here.
"""

import json
import os
import re
import queue
import subprocess
import sys
import textwrap
import threading
import time

import pymupdf
import pytest

from pdf_reader_writer_mcp import edit, pdf

EDIT = [sys.executable, "-m", "pdf_reader_writer_mcp.edit"]
SYNC_VERSION = 2  # sourcesync.js: the first protocol with sync
MIN_PROTOCOL_VERSION = 1  # editservice.js


class Service:
    """A client like editservice.js: ids from 1, fields that are None left out, one
    line per request, answers read by a thread and matched by id."""

    def __init__(self, env=None):
        self.p = subprocess.Popen(EDIT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env)
        self.seq = 0
        self.lines: queue.Queue = queue.Queue()
        self.err: list[str] = []
        threading.Thread(target=self._pump, args=(self.p.stdout, self.lines), daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(self.p.stderr), daemon=True).start()

    @staticmethod
    def _pump(stream, q):
        for line in stream:
            q.put(line)
        q.put(None)

    def line(self, op, **fields):
        self.seq += 1
        return self.seq, json.dumps({"id": self.seq, "op": op, **{k: v for k, v in fields.items() if v is not None}})

    def send(self, *lines):
        self.p.stdin.write("".join(l + "\n" for l in lines))
        self.p.stdin.flush()

    def answer(self, timeout=30):
        line = self.lines.get(timeout=timeout)
        assert line is not None, "the service closed stdout"
        return json.loads(line)

    def request(self, op, **fields):
        """One request; its result, or AssertionError with the service's message."""
        rid, line = self.line(op, **fields)
        self.send(line)
        resp = self.answer()
        assert resp["id"] == rid
        if not resp["ok"]:
            raise ServiceError(resp["error"])
        return resp["result"]

    def error(self, op, **fields):
        with pytest.raises(ServiceError) as e:
            self.request(op, **fields)
        return str(e.value)

    def close(self):
        self.p.stdin.close()
        return self.p.wait(30)


class ServiceError(Exception):
    pass


@pytest.fixture
def service():
    s = Service()
    yield s
    if s.p.poll() is None:
        assert s.close() == 0


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


def pdf_rect(path, page, *words):
    """The PDF user-space rect over ``words`` (adjacent, on one line), as pdf.js reports
    a selection rect (``viewport.convertToPdfPoint``)."""
    with pymupdf.open(path) as doc:
        pg = doc[page - 1]
        r = pymupdf.Rect()
        for w in pg.get_text("words"):
            if w[4] in words:
                r |= pymupdf.Rect(w[:4])
        return pdf.to_pdf_rect(pg, r)


def rgb_string(rgb):
    """marks.js rgbString: a snapshot's colour as the service's "r,g,b"."""
    return ",".join(f"{round(v, 4):g}" for v in rgb[:3])


# --- the process ---

def test_each_answer_is_flushed_before_the_next_request(service, sample):
    """The client waits for an answer before it sends what depends on it."""
    assert service.request("ping") == {"version": 2}
    m = service.request("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "brown", "fox")], author="David")
    r = service.request("reply", path=sample, xref=m["xref"], note="why?", author="David")
    assert [a["xref"] for a in service.request("list", path=sample)] == [m["xref"], r["xref"]]


def test_pipelined_requests_are_answered_in_order(service, sample):
    """Concurrent requests (editservice.js keeps a map of pending ids) come back in the
    order sent, each with its id, an error in between included."""
    rect = pdf_rect(sample, 1, "lazy")
    lines = [service.line("ping"), service.line("mark", path=sample, page=1, rects=[rect], author="D"),
             service.line("mark", path=sample, page=9, rects=[rect], author="D"),
             service.line("frobnicate"), service.line("list", path=sample)]
    service.send(*(l for _, l in lines))
    answers = [service.answer() for _ in lines]
    assert [a["id"] for a in answers] == [i for i, _ in lines]
    assert [a["ok"] for a in answers] == [True, True, False, False, True]
    assert answers[2]["error"] == "page 9 outside 1-2" and answers[3]["error"] == "unknown op: frobnicate"
    assert answers[4]["result"][0]["xref"] == answers[1]["result"]["xref"]


def test_protocol_negotiation(service):
    """editservice.js probe(): ping's version must be an integer >= 1; sourcesync.js
    turns sync on from 2."""
    v = service.request("ping")["version"]
    assert isinstance(v, int) and not isinstance(v, bool)
    assert v >= MIN_PROTOCOL_VERSION and v >= SYNC_VERSION and v == edit.VERSION
    assert service.error("ping", path="/x.pdf") == "unknown field: path"


def test_requests_after_a_bad_line_still_work(service):
    service.send("not json", "[1, 2]", json.dumps({"op": "ping"}), json.dumps({"id": True, "op": "ping"}), "", "   ")
    bad = [service.answer() for _ in range(4)]
    assert all(b["id"] is None and b["ok"] is False and b["error"].startswith("bad request: ") for b in bad)
    assert [b["error"] for b in bad[1:]] == ["bad request: not a JSON object", "bad request: id must be an integer",
                                            "bad request: id must be an integer"]
    assert service.request("ping") == {"version": 2}


def test_stdout_carries_only_answers(service, tmp_path):
    """MuPDF's messages go to stderr: the client would log a stray stdout line and lose
    nothing, but a message glued to an answer would lose the answer."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\n1 0 obj <</Type/Catalog>> endobj\ntrailer <</Root 1 0 R>>\n%%EOF\n")
    for op in ("list", "snapshot"):
        rid, line = service.line(op, path=str(broken), **({"xref": 1} if op == "snapshot" else {}))
        service.send(line)
        a = service.answer()
        assert a["id"] == rid
    assert service.request("ping") == {"version": 2}
    assert service.close() == 0
    assert service.lines.get(timeout=5) is None  # nothing else was written to stdout


def test_a_stray_print_goes_to_stderr(tmp_path):
    script = textwrap.dedent("""
        from pdf_reader_writer_mcp import edit
        def noisy():
            print("stray output from an op")
            return {"version": 2}
        edit.OPS["ping"] = noisy
        edit.main()
    """)
    p = subprocess.run([sys.executable, "-c", script], input='{"id": 1, "op": "ping"}\n',
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    assert [json.loads(l) for l in p.stdout.splitlines()] == [{"id": 1, "ok": True, "result": {"version": 2}}]
    assert "stray output from an op" in p.stderr


def test_eof_ends_the_service_after_the_requests_sent(service, sample):
    """dispose(): stdin is closed; the service answers what it got and exits 0."""
    lines = [service.line("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "fox")], author="D")
             for _ in range(3)]
    service.send(*(l for _, l in lines))
    assert service.close() == 0
    assert [service.answer()["ok"] for _ in lines] == [True] * 3
    assert len(pdf.annotations(sample)) == 3


def test_a_request_takes_as_long_as_the_ones_before_it(tmp_path, fake_tex):
    """The service handles one request at a time: a ping behind a slow sync is answered
    after it, which is why editservice.js times a request from the previous answer."""
    path, tex, env = fake_tex(delay=0.5)
    pdf.mark_text(path, 1, "lazy dog")
    s = Service(env)
    try:
        assert s.request("ping")["version"] == 2  # started and warm
        (sid, sync), (pid, ping) = s.line("sync", path=path), s.line("ping")
        t0 = time.monotonic()
        s.send(sync, ping)
        first, second = s.answer(), s.answer()
        assert (first["id"], second["id"]) == (sid, pid) and first["ok"] and second["ok"]
        assert time.monotonic() - t0 >= 0.5
    finally:
        assert s.close() == 0


# --- every op with the fields the client sends ---

def test_null_means_absent_for_every_optional_field(service, sample):
    """editservice.js drops undefined fields, but null is accepted the same way."""
    rect = pdf_rect(sample, 1, "fox")
    m = service.request("mark", path=sample, page=1, rects=[rect], author="D", text=None, style=None, color=None, note=None)
    assert edit.handle({"id": 1, "op": "note", "path": sample, "page": 1, "point": [50, 50], "note": "n",
                        "author": "D", "color": None})["ok"]
    for op, fields in [("mark", dict(page=1, rects=[rect], author="D", text=None, style=None, color=None, note=None)),
                       ("note", dict(page=1, point=[50, 50], note="n", author="D", color=None)),
                       ("update", dict(xref=m["xref"], note=None, color=None, style=None, only_author=None)),
                       ("delete", dict(xref=m["xref"], only_author=None))]:
        r = edit.handle({"id": 1, "op": op, "path": sample, **fields})
        assert r["ok"], (op, r)


def test_the_marking_controller_flow(service, sample):
    """marks.js: a selection over two pages is one mark per page, the note on the first;
    a margin note; a reply; then the undo stack's inverses."""
    author = "David"
    parts = [{"page": 1, "rects": [pdf_rect(sample, 1, "lazy", "dog.")], "text": "lazy dog."},
             {"page": 2, "rects": [pdf_rect(sample, 2, "Page", "2")], "text": "Page 2"}]
    marks = [service.request("mark", path=sample, author=author, style="underline", color="green",
                             note="across pages" if i == 0 else None, **p) for i, p in enumerate(parts)]
    assert [m["text"] for m in marks] == ["lazy dog.", "Page 2"] and [m["page"] for m in marks] == [1, 2]
    note = service.request("note", path=sample, page=2, point=[30, 700], note="margin", author=author, color="#ff8800")
    reply = service.request("reply", path=sample, xref=marks[0]["xref"], note="answer", author="llm")
    listed = {a["xref"]: a for a in service.request("list", path=sample)}
    assert listed[marks[0]["xref"]]["note"] == "across pages" and listed[marks[1]["xref"]]["note"] == ""
    assert listed[marks[0]["xref"]]["type"] == "Underline" and listed[note["xref"]]["type"] == "Text"
    assert listed[reply["xref"]]["irt"] == marks[0]["xref"] and listed[reply["xref"]]["author"] == "llm"
    for a in listed.values():
        assert {"page", "type", "author", "note", "modified", "xref", "irt", "quads", "rect"} <= set(a)

    # undo "create" of the first mark: the controller sees llm's reply in list and refuses;
    # the second has no replies and is deleted with only_author
    others = [a for a in listed.values() if a["irt"] == marks[0]["xref"] and a["author"] != author]
    assert others
    assert service.request("delete", path=sample, xref=marks[1]["xref"], only_author=author)["snapshots"][0]["subtype"] == "Underline"
    # someone else's annotation: only_author protects it
    assert service.error("delete", path=sample, xref=reply["xref"], only_author=author) == "not your annotation"
    assert service.error("update", path=sample, xref=reply["xref"], note="x", only_author=author) == "not your annotation"


def test_undo_of_a_note_colour_and_style_change(service, sample):
    """marks.js update(): snapshot first, then update; the inverse is an update back to
    the snapshot's contents, rgbString(color) and subtype, with only_author."""
    author = "David"
    m = service.request("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "quick", "brown")], author=author,
                        color="pink", note="")
    r = service.request("reply", path=sample, xref=m["xref"], note="r", author=author)
    before = service.request("snapshot", path=sample, xref=m["xref"])
    listed_before = {a["xref"]: a for a in service.request("list", path=sample)}[m["xref"]]

    assert service.request("update", path=sample, xref=m["xref"], note="edited", only_author=author) == {"xref": m["xref"]}
    service.request("update", path=sample, xref=m["xref"], note=before["contents"], only_author=author)
    assert service.request("snapshot", path=sample, xref=m["xref"])["contents"] == ""

    service.request("update", path=sample, xref=m["xref"], color="#123456", only_author=author)
    service.request("update", path=sample, xref=m["xref"], color=rgb_string(before["color"]), only_author=author)
    after = service.request("snapshot", path=sample, xref=m["xref"])
    assert after["color"] == before["color"]
    assert {a["xref"]: a for a in service.request("list", path=sample)}[m["xref"]]["color"] == listed_before["color"]

    styled = service.request("update", path=sample, xref=m["xref"], style="squiggly", only_author=author)["xref"]
    assert styled != m["xref"]
    back = service.request("update", path=sample, xref=styled, style="highlight", only_author=author)["xref"]
    assert back not in (styled, m["xref"])
    final = service.request("snapshot", path=sample, xref=back)
    for k in ("subtype", "quads", "color", "contents", "author", "created"):
        assert final[k] == before[k], k
    assert {a["xref"]: a["irt"] for a in service.request("list", path=sample)}[r["xref"]] == back


def test_undo_of_deletes_with_remapped_xrefs(service, sample):
    """Two deletes, undone in reverse order; marks.js remaps the xrefs of the entries
    left on the stack (UndoStack.remap) with restore's {xrefs}, keys as strings."""
    a = service.request("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "jumps")], author="D", note="a")
    ra = service.request("reply", path=sample, xref=a["xref"], note="ra", author="D")
    rra = service.request("reply", path=sample, xref=ra["xref"], note="rra", author="D")
    stack = [{"kind": "delete", "snapshots": service.request("delete", path=sample, xref=rra["xref"])["snapshots"]},
             {"kind": "delete", "snapshots": service.request("delete", path=sample, xref=a["xref"])["snapshots"]}]
    assert service.request("list", path=sample) == []
    assert [s["xref"] for s in stack[1]["snapshots"]] == [a["xref"], ra["xref"]]
    for s in stack[0]["snapshots"] + stack[1]["snapshots"]:
        assert {"xref", "page", "subtype", "rect", "quads", "color", "opacity", "contents", "author", "irt", "icon",
                "created", "modified", "nm"} <= set(s)

    xrefs = service.request("restore", path=sample, snapshots=stack.pop()["snapshots"])["xrefs"]
    assert set(xrefs) == {str(a["xref"]), str(ra["xref"])} and all(isinstance(v, int) for v in xrefs.values())
    for s in stack[0]["snapshots"]:  # UndoStack.remap
        s["irt"] = xrefs.get(str(s["irt"]), s["irt"])
    xrefs2 = service.request("restore", path=sample, snapshots=stack.pop()["snapshots"])["xrefs"]
    by = {x["xref"]: x for x in service.request("list", path=sample)}
    assert by[xrefs[str(ra["xref"])]]["irt"] == xrefs[str(a["xref"])]
    assert by[xrefs2[str(rra["xref"])]]["irt"] == xrefs[str(ra["xref"])]
    assert [by[x]["note"] for x in sorted(by)] == ["a", "ra", "rra"]


def test_restore_is_all_or_nothing_across_pages_and_kinds(service, sample):
    good = service.request("snapshot", path=sample,
                           xref=service.request("note", path=sample, page=2, point=[50, 50], note="n", author="D")["xref"])
    service.request("delete", path=sample, xref=good["xref"])
    for bad, message in [(dict(good, page=3), "page 3 outside 1-2"),
                         (dict(good, subtype="FileAttachment"), "cannot restore a FileAttachment annotation"),
                         (dict(good, subtype="Ink"), "cannot restore a Ink annotation"),
                         ({"xref": 1, "page": 1}, "each snapshot needs at least xref, page, subtype and rect"),
                         ("not a snapshot", "each snapshot needs at least xref, page, subtype and rect")]:
        assert service.error("restore", path=sample, snapshots=[good, bad]) == message
        assert service.request("list", path=sample) == []


def test_mark_errors_in_the_order_the_service_checks(service, sample, tmp_path):
    """Field checks come before the file: a bad request is reported as such even for a
    missing file or page (the vscode fake checks the file first)."""
    gone = str(tmp_path / "gone.pdf")
    rect = pdf_rect(sample, 1, "fox")
    assert service.error("mark", path=gone, page=1, rects=[[1, 2, 3]], author="D") == "rects must be a list of [x0, y0, x1, y1]"
    assert service.error("mark", path=gone, page=1, rects=[], author="D") == "rects is empty"
    assert service.error("mark", path=gone, page=1, rects=[rect], author="D", style="bold").startswith("style must be one of")
    assert service.error("mark", path=gone, page=1, rects=[rect], author="D", color="nope").startswith("unknown colour 'nope'")
    assert service.error("mark", path=gone, page=1, rects=[rect], author="D") == f"no such file: {gone}"
    assert service.error("mark", path=sample, page=0, rects=[rect], author="D") == "page 0 outside 1-2"
    assert service.error("note", path=gone, page=1, point=[1], note="n", author="D") == "point must be [x, y]"
    assert service.error("update", path=sample, xref=424242, style="bold").startswith("style must be one of")
    assert service.error("update", path=sample, xref=424242, note="n") == "no annotation with xref 424242"
    assert service.error("mark", path=sample, page=True, rects=[rect], author="D") == "field page must be an integer"
    assert service.error("mark", path=sample, page=1, rects=[[1, 2, 3, True]], author="D") == "rects must be a list of [x0, y0, x1, y1]"
    assert service.request("list", path=sample) == []


# --- sync as sourcesync.js asks for it ---

FAKE_SYNCTEX = """#!{python}
import os, sys, time
time.sleep(float(os.environ.get("FAKE_SYNCTEX_DELAY", "0")))
page, x, y, path = sys.argv[sys.argv.index("-o") + 1].split(":", 3)
print("SyncTeX result begin")
print("Input:" + os.environ["FAKE_SYNCTEX_INPUT"])
print("Line:%d" % (4 + max(0, round((float(y) - 95) / 20))))
print("SyncTeX result end")
"""

PRE = "\\documentclass{article}\n\\usepackage{pdfnotes}\n\\begin{document}\n"
LINES = ["The quick brown fox jumps over the lazy dog.", "A second line with words."]


@pytest.fixture
def fake_tex(tmp_path):
    """doc.pdf with ``LINES`` from doc.tex, a doc.synctex naming it, and a ``synctex``
    on PATH that maps a point to the source line of its PDF line (no TeX needed)."""
    def make(delay=0.0, tex=None):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "synctex"
        exe.write_text(FAKE_SYNCTEX.format(python=sys.executable))
        exe.chmod(0o755)
        texf = tmp_path / "doc.tex"
        texf.write_text(tex or PRE + "\n".join(LINES) + "\n\\end{document}\n")
        doc = pymupdf.open()
        pg = doc.new_page()
        for i, line in enumerate(LINES):
            pg.insert_text((72, 100 + 20 * i), line)
        doc.save(tmp_path / "doc.pdf"); doc.close()
        (tmp_path / "doc.synctex").write_text(f"SyncTeX Version:1\nInput:1:{texf}\n")
        env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
                   FAKE_SYNCTEX_INPUT=str(texf), FAKE_SYNCTEX_DELAY=str(delay))
        return str(tmp_path / "doc.pdf"), texf, env
    return make


def changed_files(report):
    """sourcesync.js changedFiles(): what the status bar names; empty means no message."""
    items = report["inserted"] + report["updated"] + report["removed"] + report["fallback"]
    return sorted({i["file"] for i in items})


def test_sync_after_each_mark(fake_tex):
    """The viewer syncs after every burst of writes: each sync reports what it did, in
    the files the status bar names, and a sync with nothing to do changes no file."""
    path, tex, env = fake_tex()
    s = Service(env)
    try:
        def sync():
            r = s.request("sync", path=path)
            assert set(r) == {"source", "inserted", "updated", "removed", "fallback", "skipped"}
            assert r["source"] == "doc.tex"
            for item in r["inserted"] + r["updated"] + r["removed"] + r["fallback"]:
                assert {"id", "file", "line", "macro"} <= set(item)
            return r

        m = s.request("mark", path=path, page=1, rects=[pdf_rect(path, 1, "lazy", "dog.")], author="David", note="n")
        r = sync()
        assert [(i["file"], i["line"], i["macro"]) for i in r["inserted"]] == [("doc.tex", 4, "pdfmark")]
        nid = r["inserted"][0]["id"]
        assert s.request("snapshot", path=path, xref=m["xref"])["nm"] == f"pdfnotes:{nid}"
        assert changed_files(sync()) == []

        reply = s.request("reply", path=path, xref=m["xref"], note="r", author="llm")
        r = sync()
        assert [(i["macro"], i["line"]) for i in r["inserted"]] == [("pdfnote", 4)]
        assert f"replyto={nid}]{{r}}" in tex.read_text()

        s.request("update", path=path, xref=m["xref"], note="n2", color="yellow", only_author="David")
        r = sync()
        assert [(u["id"], u["changed"]) for u in r["updated"]] == [(nid, ["note", "color"])]
        new = s.request("update", path=path, xref=m["xref"], style="strikeout", only_author="David")["xref"]
        r = sync()
        assert [(u["id"], u["changed"]) for u in r["updated"]] == [(nid, ["style"])]
        assert s.request("snapshot", path=path, xref=new)["nm"] == f"pdfnotes:{nid}"

        snaps = s.request("delete", path=path, xref=new, only_author="David")["snapshots"]
        r = sync()
        assert sorted(i["macro"] for i in r["removed"]) == ["pdfmark", "pdfnote"] and changed_files(r) == ["doc.tex"]
        assert "pdfmark" not in tex.read_text() and "pdfnote[" not in tex.read_text()
        s.request("restore", path=path, snapshots=snaps)
        r = sync()
        assert {i["id"] for i in r["inserted"]} >= {nid}
        text = tex.read_text()
        # yellow's 0.9 reads back from the PDF as 0.8999...: #ffe54d, within sync's 1/255
        assert re.search(rf"\\pdfmark\[id={nid},author=David,color=#ffe[56]4d,style=strikeout\]\{{lazy dog\.\}}\{{n2\}}", text)
        assert changed_files(sync()) == []
        assert reply  # the reply came back with its thread
    finally:
        assert s.close() == 0


def test_sync_answers_sourcesync_relies_on(fake_tex, sample):
    path, tex, env = fake_tex(tex="\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n")
    s = Service(env)
    try:
        # NO_PDFNOTES = /does not load the pdfnotes package/: the viewer stops trying until a rebuild
        message = s.error("sync", path=path)
        assert "does not load the pdfnotes package" in message and message.startswith(str(tex))
        # no SyncTeX file: not an error, a report with no source and a reason to log
        assert s.request("sync", path=sample) == {"source": None,
                                                 "reason": "no SyncTeX file next to the PDF (build with -synctex=1)"}
    finally:
        assert s.close() == 0
    env["PATH"] = os.path.dirname(sys.executable)  # no synctex anywhere
    s = Service(env)
    try:
        tex.write_text(PRE + "x\n\\end{document}\n")
        pdf.mark_text(path, 1, "lazy dog")
        assert s.error("sync", path=path) == "synctex not found on PATH (it comes with TeX Live)"
    finally:
        assert s.close() == 0


# --- the PDF changed by someone else between requests ---

@pytest.mark.parametrize("how", ["rename", "in place"])
def test_the_service_reads_the_file_afresh_after_okular_saves(service, sample, how):
    """Okular saves between two requests, rewriting the whole file: the long-lived
    service keeps no document open, so it sees Okular's note and writes on top of it."""
    m = service.request("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "fox")], author="David")
    with pymupdf.open(sample) as doc:
        a = doc[0].add_text_annot((20, 20), "from okular")
        a.set_info(title="okular")
        a.update()
        data = doc.tobytes(garbage=4)  # renumbers the objects, as a full rewrite may
    if how == "rename":
        with open(sample + ".new", "wb") as f:
            f.write(data)
        os.replace(sample + ".new", sample)
    else:
        with open(sample, "r+b") as f:
            f.truncate(0)
            f.write(data)
    listed = service.request("list", path=sample)
    assert sorted(a["author"] for a in listed) == ["David", "okular"]
    xrefs = {a["author"]: a["xref"] for a in listed}
    if xrefs["David"] != m["xref"]:
        # The rewrite renumbered the objects: the client's old xref names something else
        # now. Its only_author keeps that from touching anyone else's annotation.
        assert service.error("update", path=sample, xref=m["xref"], note="n", only_author="David") in (
            f"no annotation with xref {m['xref']}", "not your annotation")
        assert service.error("delete", path=sample, xref=m["xref"], only_author="David") in (
            f"no annotation with xref {m['xref']}", "not your annotation")
    assert {a["author"]: a["note"] for a in service.request("list", path=sample)}["okular"] == "from okular"
    service.request("update", path=sample, xref=xrefs["David"], note="after the save", only_author="David")
    service.request("mark", path=sample, page=2, rects=[pdf_rect(sample, 2, "dog.")], author="David")
    with pymupdf.open(sample) as doc:
        assert not doc.is_repaired and doc.can_save_incrementally()
    assert len(service.request("list", path=sample)) == 3


def test_page_count_changes_between_requests(service, sample):
    """A rebuild with fewer pages: the next request checks against the new count."""
    rect = pdf_rect(sample, 2, "fox")
    with pymupdf.open(sample) as doc:
        doc.delete_page(1)
        data = doc.tobytes()
    with open(sample, "wb") as f:
        f.write(data)
    assert service.error("mark", path=sample, page=2, rects=[rect], author="D") == "page 2 outside 1-1"
