"""The PDF as shared state: the MCP tools and the edit service write it under one flock,
and Okular, which takes no lock, saves it whenever the user presses Ctrl+S, by
rewriting the file in place or by renaming a new file over it."""

import fcntl
import os
import threading

import pymupdf
import pytest

from pdf_reader_writer_mcp import edit, pdf, source


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
