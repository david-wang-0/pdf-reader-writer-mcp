"""The PDF as shared state: the MCP tools and the edit service write it under one flock,
and Okular, which takes no lock, saves it whenever the user presses Ctrl+S, by
rewriting the file in place or by renaming a new file over it."""

import fcntl
import os
import subprocess
import sys
import threading
import time

import pymupdf
import pytest

from pdf_reader_writer_mcp import edit, pdf, source
from test_edit_client import Service, fake_tex, pdf_rect  # noqa: F401 (fake_tex is a fixture)


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


def okular_save(path, how, note="okular note"):
    """What Okular's Ctrl+S does to the file: the user's note added and the whole file
    rewritten, either in place or as a new file renamed over the old one."""
    with pymupdf.open(path) as doc:
        a = doc[0].add_text_annot((20, 20), note)
        a.set_info(title="okular")
        a.update()
        data = doc.tobytes(garbage=3)
    if how == "rename":
        with open(path + ".part", "wb") as f:
            f.write(data)
        os.replace(path + ".part", path)
    else:
        with open(path, "r+b") as f:
            f.truncate(0)
            f.write(data)


def notes(path):
    with pymupdf.open(path) as doc:
        assert not doc.is_repaired
        return sorted(a.info["content"] or a.type[1] for pg in doc for a in pg.annots())


# --- a save by another program in the middle of a write ---

@pytest.mark.parametrize("how", ["rename", "in place"])
@pytest.mark.parametrize("writer", ["mark_text", "edit mark"])
def test_a_save_during_a_write_is_kept_and_the_write_refused(sample, monkeypatch, how, writer):
    """Regression: our increment was saved over the other program's file: its save was
    lost (rename), or the PDF broken and repaired, refusing every later save (in place)."""
    real = pdf.add_markup

    def saved_meanwhile(*args, **kwargs):
        a = real(*args, **kwargs)
        okular_save(sample, how)
        return a
    monkeypatch.setattr(pdf, "add_markup", saved_meanwhile)
    with pytest.raises(RuntimeError, match="the PDF changed on disk while it was being written"):
        if writer == "mark_text":
            pdf.mark_text(sample, 1, "brown fox")
        else:
            r = edit.handle({"id": 1, "op": "mark", "path": sample, "page": 1, "author": "D",
                             "rects": [[100, 680, 200, 700]]})
            raise RuntimeError(r["error"])
    monkeypatch.setattr(pdf, "add_markup", real)
    assert notes(sample) == ["okular note"]
    pdf.mark_text(sample, 1, "lazy dog")  # and the next write goes on top of it
    assert notes(sample) == ["Highlight", "okular note"]


def test_a_save_during_sync_leaves_the_sources_alone(tmp_path, monkeypatch, sample):
    """The ids sync writes into the sources are saved with the PDF: neither, or both."""
    tex = tmp_path / "s.tex"
    text = "\\documentclass{article}\n\\usepackage{pdfnotes}\n\\begin{document}\nThe quick brown fox.\n\\end{document}\n"
    tex.write_text(text)
    (tmp_path / "s.synctex").write_text(f"SyncTeX Version:1\nInput:1:{tex}\n")
    pdf.mark_text(sample, 1, "brown fox")
    lookups = []

    def edit_line(path, page, x, y):
        lookups.append(page)
        okular_save(sample, "rename")  # Okular saves while the write phase looks the mark up
        return [(str(tex), 4)]
    monkeypatch.setattr(source, "synctex_edit", edit_line)
    monkeypatch.setattr(source, "lookup", lambda path, probes: {})  # every lookup in the write phase
    with pytest.raises(RuntimeError, match="changed on disk"):
        source.sync(sample)
    assert tex.read_text() == text
    assert notes(sample) == ["Highlight", "okular note"]


def test_a_writer_waiting_for_the_lock_follows_a_rename(sample, monkeypatch):
    """A writer that waited on the old file while it was renamed over must lock the new
    one: a lock on the old file excludes nobody who opens the path now."""
    held = open(sample, "rb")
    fcntl.flock(held, fcntl.LOCK_EX)
    waiting = threading.Event()
    real_lock, real_open = pdf._lock, pdf.pymupdf.open
    exclusive = []

    def lock(f, mode):
        waiting.set()
        real_lock(f, mode)

    def open_doc(path, *args, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            with open(path, "rb") as other:  # someone opening the path now
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    exclusive.append(False)
                except BlockingIOError:
                    exclusive.append(True)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(pdf, "_lock", lock)
    monkeypatch.setattr(pdf.pymupdf, "open", open_doc)
    result = {}
    t = threading.Thread(target=lambda: result.update(pdf.add_note(sample, 1, "waited")))
    t.start()
    assert waiting.wait(10)
    okular_save(sample, "rename")
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    t.join(20)
    monkeypatch.undo()
    assert result and exclusive == [True]
    assert notes(sample) == ["okular note", "waited"]


def test_a_file_removed_while_waiting(sample, monkeypatch):
    held = open(sample, "rb")
    fcntl.flock(held, fcntl.LOCK_EX)
    real_lock = pdf._lock

    def lock(f, mode):
        os.remove(sample)
        fcntl.flock(held, fcntl.LOCK_UN)
        real_lock(f, mode)
    monkeypatch.setattr(pdf, "_lock", lock)
    with pytest.raises(FileNotFoundError, match=f"no such file: {sample}"):
        pdf.add_note(sample, 1, "n")
    held.close()


# --- the MCP server and the edit service at the same time, as processes ---

N = 30

MCP_WRITER = """
import sys
from pdf_reader_writer_mcp import server
path, n = sys.argv[1], int(sys.argv[2])
print("ready", flush=True)
sys.stdin.readline()
for i in range(n):
    server.mark_text(path, 1, f"m{i}x", note=f"mcp {i}")
print("done", flush=True)
"""


@pytest.fixture
def words(tmp_path):
    """A page of distinct words e0x.. and m0x.., in a build directory, and a link to it."""
    (tmp_path / "build").mkdir()
    real = tmp_path / "build" / "w.pdf"
    doc = pymupdf.open()
    pg = doc.new_page()
    for row, prefix in enumerate("em"):
        for i in range(N):
            pg.insert_text((40 + 36 * (i % 14), 60 + 220 * row + 14 * (i // 14)), f"{prefix}{i}x", fontsize=9)
    doc.save(real); doc.close()
    link = tmp_path / "w.pdf"
    link.symlink_to(real)
    return str(link), str(real)


def test_mcp_by_link_and_edit_service_by_realpath(words):
    """Okular's path for the model, the VS Code viewer's realpath for the user: both
    writers lock the one file, so all 2N marks land and the file opens without repair."""
    link, real = words
    s = Service()
    w = subprocess.Popen([sys.executable, "-c", MCP_WRITER, link, str(N)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert s.request("ping")["version"] == 2 and w.stdout.readline().strip() == "ready"
        lines = [s.line("mark", path=real, page=1, rects=[pdf_rect(real, 1, f"e{i}x")], author="e")[1] for i in range(N)]
        s.send(*lines)
        w.stdin.write("go\n"); w.stdin.flush()
        assert all(s.answer()["ok"] for _ in range(N))
        out, err = w.communicate(timeout=120)
        assert w.returncode == 0 and out.strip() == "done", err
    finally:
        assert s.close() == 0
    assert notes(real).count("Highlight") == N
    texts = {a["text"] for a in pdf.annotations(link)}
    assert texts == {f"{p}{i}x" for p in "em" for i in range(N)}


def test_marks_made_while_a_sync_runs(fake_tex):
    """sourcesync.js syncs after the user's burst of marks; the model marks meanwhile
    through the MCP server. The sync's SyncTeX lookups (slowed down here) hold no lock,
    so the model's marks wait for nothing and none is lost; the next sync takes them."""
    path, tex, env = fake_tex(delay=0.15)
    s = Service(env)
    try:
        for word in ("quick", "brown", "lazy"):
            s.request("mark", path=path, page=1, rects=[pdf_rect(path, 1, word)], author="David")
        rid, line = s.line("sync", path=path)
        s.send(line)
        t0 = time.monotonic()
        model = [pdf.mark_text(path, 1, w, note="model") for w in ("second", "line", "words")]
        assert time.monotonic() - t0 < 0.5 * 3  # not stuck behind the sync's lookups
        first = s.answer()
        assert first["id"] == rid and first["ok"], first
        second = s.request("sync", path=path)
        inserted = len(first["result"]["inserted"]) + len(second["inserted"])
        assert inserted == 6 and s.request("sync", path=path)["inserted"] == []
    finally:
        assert s.close() == 0
    assert len(model) == 3 and len(notes(path)) == 6
    assert tex.read_text().count("\\pdfmark[") == 6
