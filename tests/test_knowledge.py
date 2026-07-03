import sys
import time

from autoresearch.knowledge import KnowledgeStore, knowledge_prompt_block
from autoresearch.sandbox import is_editable_path

from conftest import make_config


def minimal_pdf(text: str) -> bytes:
    """Hand-crafted single-page PDF with real extractable text."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF").encode()
    return bytes(out)


def test_add_text_and_index(store, workspace):
    exp = store.create(make_config(workspace), "")
    ks = KnowledgeStore(exp.dir)
    entry = ks.add("notes.md", b"# Momentum trick\nUse Nesterov momentum for faster convergence.")
    assert entry["kind"] == "text" and entry["chars"] > 0
    docs = ks.list()
    assert [d["name"] for d in docs] == ["notes.md"]
    index = (ks.dir / "INDEX.md").read_text()
    assert "notes.md" in index and "Momentum trick" in index
    block = knowledge_prompt_block(ks)
    assert "knowledge/INDEX.md" in block and "notes.md" in block


def test_add_pdf_extracts_text(store, workspace):
    exp = store.create(make_config(workspace), "")
    ks = KnowledgeStore(exp.dir)
    entry = ks.add("paper.pdf", minimal_pdf("Nearest neighbour beats input order"))
    assert entry["kind"] == "pdf"
    assert entry["extracted"] == "paper.extracted.md"
    extracted = (ks.dir / "paper.extracted.md").read_text()
    assert "Nearest neighbour beats input order" in extracted
    assert "paper.extracted.md" in (ks.dir / "INDEX.md").read_text()


def test_duplicate_names_and_remove(store, workspace):
    exp = store.create(make_config(workspace), "")
    ks = KnowledgeStore(exp.dir)
    ks.add("a.txt", b"one")
    entry2 = ks.add("a.txt", b"two")
    assert entry2["name"] == "a-2.txt"
    ks.remove("a-2.txt")
    assert [d["name"] for d in ks.list()] == ["a.txt"]
    ks.remove("a.txt")
    assert ks.list() == [] and not ks.dir.exists()
    assert knowledge_prompt_block(ks) == ""


def scanned_pdf(text: str) -> bytes:
    """An image-only PDF (no text layer) — as if it came from a scanner."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.text((60, 150), text, fill="black", font=ImageFont.load_default(size=48))
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def test_scanned_pdf_falls_back_to_ocr(store, workspace):
    import shutil as sh
    import pytest
    if not sh.which("tesseract"):
        pytest.skip("tesseract not installed")
    exp = store.create(make_config(workspace), "")
    ks = KnowledgeStore(exp.dir)
    entry = ks.add("scan.pdf", scanned_pdf("OCR RECOVERS THIS SENTENCE"))
    assert entry["extracted"] == "scan.extracted.md", entry["note"]
    assert "OCR" in (entry["note"] or "")
    extracted = (ks.dir / "scan.extracted.md").read_text()
    assert "RECOVERS" in extracted.upper()


def test_knowledge_never_editable():
    assert not is_editable_path("knowledge/paper.pdf", ["**"])
    assert not is_editable_path("knowledge/INDEX.md", ["knowledge/*"])
    assert is_editable_path("solution.py", ["solution.py"])


def test_knowledge_in_workdir_and_tamper_reverted(store, workspace):
    """Knowledge reaches the agent's working copy and survives tampering."""
    tamper = ("import pathlib, os;"
              "p = pathlib.Path('knowledge/facts.txt');"
              "os.chmod(p, 0o644); p.write_text('LIES')")
    cfg = make_config(
        workspace,
        editable_files=["solution.py", "knowledge/*"],  # even a hostile whitelist
        agent={"type": "command", "command_template": f'{sys.executable} -c "{tamper}"'},
    )
    exp = store.create(cfg, "")
    ks = KnowledgeStore(exp.dir)
    ks.add("facts.txt", b"TRUTH")

    from autoresearch.loop import ResearchLoop
    from autoresearch.events import EventBus
    loop = ResearchLoop(exp, EventBus())
    loop.start()
    deadline = time.time() + 60
    while loop.running and time.time() < deadline:
        time.sleep(0.05)

    assert (exp.work_dir / "knowledge" / "facts.txt").read_text() == "TRUTH"
    meta = exp.iteration_meta(1)
    assert any(v["path"] == "knowledge/facts.txt" for v in meta["sandbox"]["violations"])


def test_knowledge_endpoints(store, workspace, monkeypatch):
    from fastapi.testclient import TestClient
    import autoresearch.server as server
    monkeypatch.setattr(server, "store", store)
    client = TestClient(server.app)

    exp = store.create(make_config(workspace), "")
    r = client.post(f"/api/experiments/{exp.id}/knowledge",
                    files={"file": ("paper.pdf", minimal_pdf("hello"), "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["document"]["extracted"] == "paper.extracted.md"

    r = client.get(f"/api/experiments/{exp.id}/knowledge")
    assert [d["name"] for d in r.json()["documents"]] == ["paper.pdf"]

    assert client.delete(f"/api/experiments/{exp.id}/knowledge/paper.pdf").status_code == 200
    assert client.get(f"/api/experiments/{exp.id}/knowledge").json()["documents"] == []
