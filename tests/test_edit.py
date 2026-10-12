import fcntl
import json
import subprocess
import sys
import textwrap

import pymupdf
import pytest

from pdf_reader_writer_mcp import edit, pdf, server

EDIT = [sys.executable, "-m", "pdf_reader_writer_mcp.edit"]


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


def call(op, **fields):
    """One request through the dispatcher; returns the result or raises with the error."""
    resp = edit.handle({"id": 7, "op": op, **fields})
    assert resp["id"] == 7
    if not resp["ok"]:
        raise AssertionError(resp["error"])
    return resp["result"]


def error(op, **fields):
    resp = edit.handle({"id": 1, "op": op, **fields})
    assert resp["ok"] is False
    return resp["error"]


def pdf_rect(path, page, word):
    """The PDF user-space rect (origin bottom-left) of ``word``, as pdf.js would report it."""
    with pymupdf.open(path) as doc:
        pg = doc[page - 1]
        w = next(w for w in pg.get_text("words") if w[4] == word)
        return pdf.to_pdf_rect(pg, pymupdf.Rect(w[:4]))


def raw(path, xref, key):
    with pymupdf.open(path) as doc:
        return doc.xref_get_key(xref, key)


def span(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


# --- mark ---

def test_mark_snaps_to_words_one_quad_per_line(sample):
    # a drag from "first" on line 1 to "quick" on line 2: two selection rects, one per line
    r1 = span(pdf_rect(sample, 1, "first"), pdf_rect(sample, 1, "text."))
    r2 = span(pdf_rect(sample, 1, "The"), pdf_rect(sample, 1, "quick"))
    r1[0] -= 1; r2[2] += 1  # the selection overshoots a little
    out = call("mark", path=sample, page=1, rects=[r1, r2], author="David", style="underline",
               color="green", note="why?")
    assert out["page"] == 1 and out["text"] == "first line of text. The quick"
    assert len(out["quads"]) == 2 and all(len(q) == 8 for q in out["quads"])
    (a,) = pdf.annotations(sample)
    assert a["type"] == "Underline" and a["author"] == "David" and a["note"] == "why?"
    assert a["text"] == "first line of text. The quick"
    assert a["color"] == [round(c, 2) for c in pdf.COLORS["green"]]


def test_mark_quads_are_pdf_user_space_and_match_quadpoints(sample):
    r = pdf_rect(sample, 1, "lazy")
    out = call("mark", path=sample, page=1, rects=[r], author="David")
    (q,) = out["quads"]
    # UL, UR, LL, LR with y up: the upper edge has the larger y
    assert q[1] == q[3] > q[5] == q[7] and q[0] == q[4] < q[2] == q[6]
    assert abs(q[0] - r[0]) < 0.01 and abs(q[1] - r[3]) < 0.01
    kind, value = raw(sample, out["xref"], "QuadPoints")
    assert kind == "array"
    assert [round(float(x), 2) for x in value.strip("[]").split()] == [round(x, 2) for x in q]


def test_mark_without_text_under_rects_uses_rects(sample):
    out = call("mark", path=sample, page=1, rects=[[300, 300, 400, 320]], author="David", text="figure")
    assert out["text"] == "figure"
    assert out["quads"] == [[300, 320, 400, 320, 300, 300, 400, 300]]


def test_mark_on_rotated_page(tmp_path):
    path = str(tmp_path / "r.pdf")
    doc = pymupdf.open()
    pg = doc.new_page(width=600, height=800)
    pg.insert_text((72, 100), "Rotated words to mark.")
    pg.set_rotation(90)
    doc.save(path); doc.close()
    out = call("mark", path=path, page=1, rects=[pdf_rect(path, 1, "words")], author="David")
    assert out["text"] == "words"
    assert pdf.annotations(path)[0]["text"] == "words"


def test_mark_errors(sample, tmp_path):
    r = pdf_rect(sample, 1, "fox")
    assert error("mark", path=sample, page=3, rects=[r], author="D") == "page 3 outside 1-2"
    gone = str(tmp_path / "gone.pdf")
    assert error("mark", path=gone, page=1, rects=[r], author="D") == f"no such file: {gone}"
    assert error("mark", path=sample, page=1, rects=[r]) == "missing field: author"
    assert error("mark", path=sample, page=1, rects=[[1, 2, 3]], author="D") == "rects must be a list of [x0, y0, x1, y1]"
    assert error("mark", path=sample, page=1, rects=[], author="D") == "rects is empty"
    assert error("mark", path=sample, page="1", rects=[r], author="D") == "field page must be an integer"
    assert error("mark", path=sample, page=1, rects=[r], author="D", style="bold").startswith("style must be one of")
    assert error("mark", path=sample, page=1, rects=[r], author="D", colour="red") == "unknown field: colour"
    assert error("mark", path=sample, page=1, rects=[r], author="D", color="chartreuse").startswith("unknown colour")
    assert pdf.annotations(sample) == []


# --- note, reply ---

def test_note_at_point(sample):
    out = call("note", path=sample, page=2, point=[100, 500], note="here", author="David", color="orange")
    assert out == {"xref": out["xref"], "page": 2}
    (a,) = pdf.annotations(sample, geometry=True)
    assert a["type"] == "Text" and a["note"] == "here" and a["author"] == "David"
    assert a["rect"][0] == 100 and a["rect"][3] == 500  # the icon hangs from the point
    assert error("note", path=sample, page=2, point=[1], note="n", author="D") == "point must be [x, y]"


def test_reply_threads_like_add_note(sample):
    root = call("mark", path=sample, page=2, rects=[pdf_rect(sample, 2, "fox")], author="David")
    r = call("reply", path=sample, xref=root["xref"], note="answer", author="llm")
    assert r["page"] == 2
    by = {a["xref"]: a for a in pdf.annotations(sample, geometry=True)}
    assert by[r["xref"]]["irt"] == root["xref"] and by[r["xref"]]["reply_to"] == root["xref"]
    assert error("reply", path=sample, xref=999999, note="n", author="D") == "no annotation with xref 999999"


# --- update ---

def test_update_note_and_color_in_place(sample):
    x = call("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "fox")], author="David")["xref"]
    assert call("update", path=sample, xref=x, note="edited", color="#ff0000") == {"xref": x}
    (a,) = pdf.annotations(sample)
    assert a["note"] == "edited" and a["color"] == [1.0, 0.0, 0.0] and a["text"] == "fox"
    assert error("update", path=sample, xref=x, note="no", only_author="llm") == "not your annotation"
    assert call("update", path=sample, xref=x, note="mine", only_author="David") == {"xref": x}


def test_update_style_recreates_and_keeps_the_thread(sample):
    root = call("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "brown"), pdf_rect(sample, 1, "fox")],
                author="David", note="root", color="pink")
    reply = call("reply", path=sample, xref=root["xref"], note="r1", author="llm")
    nested = call("reply", path=sample, xref=reply["xref"], note="r2", author="David")
    before = call("snapshot", path=sample, xref=root["xref"])
    new = call("update", path=sample, xref=root["xref"], style="strikeout")["xref"]
    assert new != root["xref"]
    by = {a["xref"]: a for a in pdf.annotations(sample, geometry=True)}
    assert root["xref"] not in by and set(by) == {new, reply["xref"], nested["xref"]}
    assert by[new]["type"] == "StrikeOut" and by[new]["note"] == "root" and by[new]["author"] == "David"
    assert by[new]["text"] == "brown fox" and by[new]["quads"] == before["quads"]
    assert by[new]["color"] == [round(c, 2) for c in pdf.COLORS["pink"]]
    assert by[reply["xref"]]["irt"] == new and by[nested["xref"]]["irt"] == reply["xref"]
    after = call("snapshot", path=sample, xref=new)
    assert after["created"] == before["created"]
    n = call("note", path=sample, page=1, point=[50, 50], note="n", author="D")["xref"]
    assert error("update", path=sample, xref=n, style="underline") == f"xref {n} is a Text, not a text markup"


# --- delete, snapshot, restore ---

def test_delete_takes_the_whole_thread_and_restore_brings_it_back(sample):
    root = call("mark", path=sample, page=2, rects=[pdf_rect(sample, 2, "lazy")], author="David",
                note="root", style="squiggly", color="purple")
    r1 = call("reply", path=sample, xref=root["xref"], note="r1", author="llm")
    r2 = call("reply", path=sample, xref=r1["xref"], note="r2", author="David")
    other = call("note", path=sample, page=2, point=[400, 400], note="unrelated", author="David")
    before = {a["xref"]: a for a in pdf.annotations(sample, geometry=True)}
    out = call("delete", path=sample, xref=root["xref"])
    snaps = out["snapshots"]
    assert [s["xref"] for s in snaps] == [root["xref"], r1["xref"], r2["xref"]]  # parent first
    assert [a["xref"] for a in pdf.annotations(sample)] == [other["xref"]]  # no orphaned r2
    xrefs = call("restore", path=sample, snapshots=snaps)["xrefs"]
    assert set(xrefs) == {str(s["xref"]) for s in snaps}
    after = {a["xref"]: a for a in pdf.annotations(sample, geometry=True)}
    for old in (root["xref"], r1["xref"], r2["xref"]):
        a, b = before[old], after[xrefs[str(old)]]
        for k in ("page", "type", "author", "note", "color", "quads", "modified"):
            assert a[k] == b[k], (k, a[k], b[k])
        assert b["irt"] == (xrefs[str(a["irt"])] if a["irt"] else 0)
    assert after[xrefs[str(root["xref"])]]["text"] == "lazy"


def test_delete_only_author(sample):
    x = call("note", path=sample, page=1, point=[50, 50], note="n", author="llm")["xref"]
    assert error("delete", path=sample, xref=x, only_author="David") == "not your annotation"
    assert len(pdf.annotations(sample)) == 1
    assert error("delete", path=sample, xref=424242) == "no annotation with xref 424242"


def test_snapshot_and_restore_file_attachment(sample):
    a = pdf.attach_file(sample, 1, "notes.md", "# Notes\n", note="summary", color="cyan")
    snap = call("snapshot", path=sample, xref=a["xref"])
    assert snap["subtype"] == "FileAttachment" and snap["file"]["name"] == "notes.md"
    call("delete", path=sample, xref=a["xref"])
    new = call("restore", path=sample, snapshots=[snap])["xrefs"][str(a["xref"])]
    back = pdf.read_attachment(sample, new)
    assert back["content"] == "# Notes\n" and back["name"] == "notes.md" and back["note"] == "summary"


def test_restore_is_all_or_nothing(sample):
    good = call("snapshot", path=sample, xref=pdf.add_note(sample, 1, "n")["xref"])
    call("delete", path=sample, xref=good["xref"])
    ink = dict(good, xref=99, subtype="Ink")
    assert error("restore", path=sample, snapshots=[good, ink]) == "cannot restore a Ink annotation"
    assert pdf.annotations(sample) == []
    assert error("restore", path=sample, snapshots=[{"xref": 1}]).startswith("each snapshot needs")


def test_list_adds_geometry_to_the_mcp_records(sample):
    m = call("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "fox")], author="David")
    (entry,) = call("list", path=sample)
    (mcp_entry,) = pdf.annotations(sample)
    assert {k: entry[k] for k in mcp_entry} == mcp_entry
    assert entry["irt"] == 0 and entry["quads"] == m["quads"] and len(entry["rect"]) == 4


def test_ping_and_request_errors():
    assert call("ping") == {"version": 1}
    assert edit.handle([1]) == {"id": None, "ok": False, "error": "bad request: not a JSON object"}
    assert edit.handle({"op": "ping"})["error"] == "bad request: id must be an integer"
    assert error("ping", extra=1) == "unknown field: extra"
    assert edit.handle({"id": 3})["error"] == "missing field: op"
    assert edit.handle({"id": 4, "op": "frobnicate"}) == {"id": 4, "ok": False, "error": "unknown op: frobnicate"}


# --- what Okular reads ---

def test_written_objects_in_okular_terms(sample):
    m = call("mark", path=sample, page=1, rects=[pdf_rect(sample, 1, "fox")], author="David", note="nb")
    r = call("reply", path=sample, xref=m["xref"], note="re", author="llm")
    assert raw(sample, m["xref"], "Subtype") == ("name", "/Highlight")
    assert raw(sample, m["xref"], "T") == ("string", "David")
    assert raw(sample, m["xref"], "Contents") == ("string", "nb")
    assert raw(sample, m["xref"], "QuadPoints")[0] == "array"
    assert raw(sample, r["xref"], "Subtype") == ("name", "/Text")
    assert raw(sample, r["xref"], "IRT") == ("xref", f"{m['xref']} 0 R")


# --- the process ---

def test_process_protocol(sample):
    lines = [json.dumps({"id": 1, "op": "ping"}), "not json", "",
             json.dumps({"id": 2, "op": "mark", "path": sample, "page": 1,
                         "rects": [pdf_rect(sample, 1, "fox")], "author": "David"}),
             json.dumps({"id": 3, "op": "list", "path": sample})]
    p = subprocess.run(EDIT, input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
    out = [json.loads(l) for l in p.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, None, 2, 3]  # in order, blank lines skipped
    assert out[0]["result"] == {"version": 1} and out[1]["error"].startswith("bad request:")
    assert out[3]["result"][0]["xref"] == out[2]["result"]["xref"]


# --- locking ---

def test_writer_times_out_on_a_held_lock(sample, monkeypatch):
    monkeypatch.setattr(pdf, "LOCK_TIMEOUT", 0.2)
    with open(sample, "rb") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError, match="PDF is locked by another writer"):
            pdf.mark_text(sample, 1, "fox")
        assert error("note", path=sample, page=1, point=[1, 1], note="n", author="D") == "PDF is locked by another writer"
        with pytest.raises(TimeoutError):
            pdf.page_text(sample, 1)  # readers wait for a writer too
    assert pdf.annotations(sample) == []


N = 40

MCP_WRITER = textwrap.dedent("""
    import sys
    from pdf_reader_writer_mcp import server
    path, n = sys.argv[1], int(sys.argv[2])
    print("ready", flush=True)
    sys.stdin.readline()  # start together with the other writer
    for i in range(n):
        server.mark_text(path, 1, f"m{i}x", note=f"mcp {i}") if i % 2 else server.add_note(path, f"mcp {i}", page=1, near_text=f"m{i}x")
""")


@pytest.fixture
def words(tmp_path):
    """A page of distinct words e0x.., f0x.., m0x.. for concurrent writers to mark."""
    path = tmp_path / "w.pdf"
    doc = pymupdf.open()
    pg = doc.new_page()
    for row, prefix in enumerate("efm"):
        for i in range(N):
            pg.insert_text((40 + 36 * (i % 14), 60 + 220 * row + 14 * (i // 14)), f"{prefix}{i}x", fontsize=9)
    doc.save(path); doc.close()
    return str(path)


def _service_input(path, prefix):
    return "".join(json.dumps({"id": i, "op": "mark", "path": path, "page": 1,
                               "rects": [pdf_rect(path, 1, f"{prefix}{i}x")], "author": prefix}) + "\n"
                   for i in range(N))


def _run_concurrently(cmds_and_inputs):
    """Start every writer, wait until each has imported everything and answered once
    (the edit service a ping, the MCP writer "ready"), then release them together:
    with staggered starts the writes would barely overlap and an unlocked writer
    would pass."""
    procs = [subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for cmd, _ in cmds_and_inputs]
    for p, (cmd, _) in zip(procs, cmds_and_inputs):
        if cmd == EDIT:
            p.stdin.write(json.dumps({"id": -1, "op": "ping"}) + "\n"); p.stdin.flush()
        assert p.stdout.readline().strip()  # the ping response / "ready"
    for p, (_, inp) in zip(procs, cmds_and_inputs):
        p.stdin.write(inp + "\n"); p.stdin.flush()
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outs.append(out)
    return outs


def _assert_clean(path, expected):
    with pymupdf.open(path) as doc:
        assert not doc.is_repaired
        assert sum(1 for _ in doc[0].annots()) == expected
    assert len({a["xref"] for a in pdf.annotations(path)}) == expected


def test_concurrent_edit_service_and_mcp_writers(words):
    out, _ = _run_concurrently([(EDIT, _service_input(words, "e")),
                                ([sys.executable, "-c", MCP_WRITER, words, str(N)], "")])
    assert len(out.splitlines()) == N and all(json.loads(l)["ok"] for l in out.splitlines())
    _assert_clean(words, 2 * N)


def test_concurrent_edit_services(words):
    outs = _run_concurrently([(EDIT, _service_input(words, "e")), (EDIT, _service_input(words, "f"))])
    assert all(len(out.splitlines()) == N and all(json.loads(l)["ok"] for l in out.splitlines()) for out in outs)
    _assert_clean(words, 2 * N)
    texts = {a["text"] for a in pdf.annotations(words)}
    assert texts == {f"{p}{i}x" for p in "ef" for i in range(N)}


def test_mcp_write_tools_take_the_lock(sample, monkeypatch):
    monkeypatch.setattr(pdf, "LOCK_TIMEOUT", 0.2)
    with open(sample, "rb") as f:
        fcntl.flock(f, fcntl.LOCK_SH)  # a reader holds the file: writers wait, then give up
        for call_ in (lambda: server.mark_text(sample, 1, "fox"), lambda: server.add_note(sample, "n", page=1),
                      lambda: server.attach_file(sample, "a.md", "x", page=1),
                      lambda: server.remove_annotation(sample, 1)):
            with pytest.raises(TimeoutError, match="PDF is locked by another writer"):
                call_()
