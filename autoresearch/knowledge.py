"""Knowledge library: reference material the agent can consult.

Design (deliberately *agentic retrieval*, not embedding-RAG):

- Uploaded files live in a read-only ``knowledge/`` folder inside the
  workspace. The sandbox treats that folder as permanently non-editable,
  so the agent can read but never tamper with its sources.
- PDFs are converted to a sidecar ``<name>.extracted.md`` at upload time,
  so even text-only agents (local models via the tool loop) can grep them.
- An auto-generated ``knowledge/INDEX.md`` gives progressive disclosure:
  the prompt carries only a short pointer + the index stays tiny, and the
  agent greps/reads full documents on demand. For corpora up to hundreds
  of documents this beats a vector index on fidelity (no chunking loss),
  freshness (no re-embedding), and infrastructure (none).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

KNOWLEDGE_DIR = "knowledge"          # reserved folder inside the workspace
INDEX_FILE = "INDEX.md"
MAX_FILE_MB = 50
MAX_OCR_PAGES = 40                   # bound OCR time on huge scans
OCR_DPI = 200
TESSDATA_CACHE = Path.home() / ".cache" / "autoresearch" / "tessdata"
TESSDATA_ENG_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata"
TEXT_SUFFIXES = {".md", ".txt", ".rst", ".csv", ".json", ".yaml", ".yml", ".tex", ".html"}
PREVIEW_CHARS = 280


def _safe_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "file"
    return name[:120]


class KnowledgeStore:
    """Manages the knowledge folder of one experiment (inside baseline/,
    so sandbox restore, version zips and workdir sync all just work)."""

    def __init__(self, experiment_dir: Path):
        self.exp_dir = Path(experiment_dir)
        self.dir = self.exp_dir / "baseline" / KNOWLEDGE_DIR
        self.meta_path = self.exp_dir / "knowledge.json"

    # -- metadata ---------------------------------------------------------
    def _load_meta(self) -> list[dict]:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text())
        return []

    def _save_meta(self, meta: list[dict]) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2))

    def list(self) -> list[dict]:
        return self._load_meta()

    def has_documents(self) -> bool:
        return bool(self._load_meta())

    # -- mutations ----------------------------------------------------------
    def add(self, filename: str, data: bytes) -> dict:
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise ValueError(f"file exceeds {MAX_FILE_MB} MB")
        name = _safe_name(filename)
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / name
        stem, suffix = target.stem, target.suffix
        n = 1
        while target.exists():
            n += 1
            target = self.dir / f"{stem}-{n}{suffix}"
        target.write_bytes(data)

        entry = {
            "name": target.name,
            "size": len(data),
            "added_at": time.time(),
            "kind": "pdf" if suffix.lower() == ".pdf" else "text",
            "extracted": None,
            "chars": None,
            "note": None,
        }
        if suffix.lower() == ".pdf":
            extracted, note = self._extract_pdf(target)
            entry["extracted"] = extracted
            entry["note"] = note
            if extracted:
                entry["chars"] = len((self.dir / extracted).read_text(errors="replace"))
        elif suffix.lower() in TEXT_SUFFIXES or self._looks_texty(data):
            entry["chars"] = len(data.decode("utf-8", errors="replace"))
        else:
            entry["note"] = "binary file — the agent can read it only if its tools support the format"

        meta = self._load_meta()
        meta.append(entry)
        self._save_meta(meta)
        self.rebuild_index()
        return entry

    def remove(self, name: str) -> None:
        meta = self._load_meta()
        entry = next((e for e in meta if e["name"] == name), None)
        if entry is None:
            raise KeyError(f"no knowledge file named {name!r}")
        for candidate in [entry["name"], entry.get("extracted")]:
            if candidate:
                p = self.dir / candidate
                if p.exists():
                    p.unlink()
        meta = [e for e in meta if e["name"] != name]
        self._save_meta(meta)
        if meta:
            self.rebuild_index()
        else:
            shutil.rmtree(self.dir, ignore_errors=True)

    # -- extraction ------------------------------------------------------------
    @staticmethod
    def _looks_texty(data: bytes) -> bool:
        sample = data[:4096]
        return b"\x00" not in sample

    def _extract_pdf(self, pdf_path: Path) -> tuple[str | None, str | None]:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_path))
            pages = []
            for i, page in enumerate(reader.pages):
                text = (page.extract_text() or "").strip()
                if text:
                    pages.append(f"<!-- page {i + 1} -->\n{text}")
            body = "\n\n".join(pages).strip()
        except Exception as exc:
            return None, f"PDF text extraction failed: {exc}"

        note = None
        if not body:
            # no text layer → scanned document → OCR fallback
            body, note = _ocr_pdf(pdf_path)
            if not body:
                return None, note
        out = pdf_path.with_suffix("").name + ".extracted.md"
        (self.dir / out).write_text(f"# Extracted from {pdf_path.name}\n\n{body}\n")
        return out, note

    # -- index (progressive disclosure for the agent) -----------------------------
    # (OCR helpers are module-level functions below)
    def rebuild_index(self) -> None:
        meta = self._load_meta()
        if not meta:
            return
        lines = [
            "# Knowledge library index",
            "",
            "Reference material provided by the user. Read the relevant files",
            "before relying on your own assumptions; cite which document an idea",
            "came from in your summary. This folder is read-only.",
            "",
        ]
        for e in sorted(meta, key=lambda x: x["name"].lower()):
            best = e.get("extracted") or e["name"]
            preview = self._preview(self.dir / best)
            size_kb = e["size"] / 1024
            lines.append(f"## {e['name']}  ({size_kb:.0f} KB)")
            if e.get("extracted"):
                lines.append(f"- plain text: `{KNOWLEDGE_DIR}/{e['extracted']}`")
            if e.get("note"):
                lines.append(f"- note: {e['note']}")
            if preview:
                lines.append(f"- begins: {preview}")
            lines.append("")
        (self.dir / INDEX_FILE).write_text("\n".join(lines))

    @staticmethod
    def _preview(path: Path) -> str | None:
        try:
            text = path.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            return None
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


# -- OCR fallback (scanned PDFs) -------------------------------------------------

def _tesseract_env() -> tuple[str, dict] | tuple[None, str]:
    """(tesseract_binary, env) if OCR is usable, else (None, reason)."""
    tess = shutil.which("tesseract")
    if not tess:
        return None, ("PDF has no text layer (scanned images) and tesseract is not "
                      "installed — install it to enable OCR")
    env = os.environ.copy()
    try:
        langs = subprocess.run([tess, "--list-langs"], capture_output=True,
                               text=True, timeout=20, env=env).stdout
    except (subprocess.SubprocessError, OSError):
        langs = ""
    if "eng" in langs.split():
        return tess, env
    # no English pack in the system tessdata → provision one in the user cache
    cached = TESSDATA_CACHE / "eng.traineddata"
    if not cached.exists():
        try:
            import httpx
            TESSDATA_CACHE.mkdir(parents=True, exist_ok=True)
            resp = httpx.get(TESSDATA_ENG_URL, follow_redirects=True, timeout=120)
            resp.raise_for_status()
            cached.write_bytes(resp.content)
        except Exception as exc:
            return None, ("PDF has no text layer; tesseract lacks English data and "
                          f"downloading it failed ({exc})")
    env["TESSDATA_PREFIX"] = str(TESSDATA_CACHE)
    return tess, env


def _ocr_pdf(pdf_path: Path) -> tuple[str, str | None] | tuple[None, str]:
    """OCR a scanned PDF. Returns (markdown_body, note) or (None, reason)."""
    tess, env_or_reason = _tesseract_env()
    if tess is None:
        return None, env_or_reason
    env = env_or_reason
    try:
        import fitz  # pymupdf — renders pages without system poppler
        doc = fitz.open(str(pdf_path))
        n_pages = min(len(doc), MAX_OCR_PAGES)
        pages = []
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(n_pages):
                png = Path(tmp) / f"page-{i + 1}.png"
                doc[i].get_pixmap(dpi=OCR_DPI).save(str(png))
                proc = subprocess.run([tess, str(png), "stdout", "-l", "eng"],
                                      capture_output=True, text=True,
                                      timeout=120, env=env)
                text = proc.stdout.strip()
                if text:
                    pages.append(f"<!-- page {i + 1} (OCR) -->\n{text}")
        body = "\n\n".join(pages).strip()
        if not body:
            return None, "OCR found no readable text in this PDF"
        note = "text recovered via OCR (scanned PDF) — expect some recognition errors"
        if len(doc) > MAX_OCR_PAGES:
            note += f"; only the first {MAX_OCR_PAGES} of {len(doc)} pages were OCRed"
        return body, note
    except Exception as exc:
        return None, f"OCR failed: {exc}"


def knowledge_prompt_block(store: KnowledgeStore) -> str:
    docs = store.list()
    if not docs:
        return ""
    names = ", ".join(f"`{e['name']}`" for e in docs[:12])
    more = f" and {len(docs) - 12} more" if len(docs) > 12 else ""
    return (
        f"A read-only knowledge library is mounted at `{KNOWLEDGE_DIR}/` "
        f"({len(docs)} document{'s' if len(docs) != 1 else ''}: {names}{more}).\n"
        f"Start with `{KNOWLEDGE_DIR}/{INDEX_FILE}`, then read or grep the relevant "
        "documents before designing your change — ground your approach in this "
        "material when applicable and mention which document informed it."
    )
