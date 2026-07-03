"""Experiment storage.

Directory layout (one directory per experiment, self-contained):

    experiments/<id>/
      experiment.json        config + status
      instructions.md        user instructions given to the agent
      baseline/              pristine snapshot of the user's workspace
      work/                  live working copy (agent + eval run here)
      champion/
        champion.json        which iteration is champion + its metrics
        files/               editable-file snapshot of the champion
      iterations/0001/
        meta.json            metrics, timings, violations, champion flag
        agent.log            full agent transcript
        summary.md           agent's own description of the change
        changes.diff         unified diff vs the previous champion
        files/               editable-file snapshot of this version
      history.jsonl          one line per iteration (drives the chart)

Every version stays downloadable: a full workspace for iteration N is
baseline + iterations/N/files (non-editable files can never change).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from .config import ExperimentConfig
from .sandbox import is_editable_path, walk_files

DATA_DIR = Path(os.environ.get("AUTORESEARCH_DATA_DIR", Path(__file__).resolve().parent.parent / "experiments"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "experiment"
    return s[:40]


class Experiment:
    def __init__(self, exp_dir: Path):
        self.dir = Path(exp_dir)
        self.id = self.dir.name
        self._lock = threading.Lock()

    # -- paths ----------------------------------------------------------
    @property
    def baseline_dir(self) -> Path: return self.dir / "baseline"
    @property
    def work_dir(self) -> Path: return self.dir / "work"
    @property
    def champion_dir(self) -> Path: return self.dir / "champion"
    @property
    def champion_files(self) -> Path: return self.champion_dir / "files"
    @property
    def iterations_dir(self) -> Path: return self.dir / "iterations"
    @property
    def instructions_path(self) -> Path: return self.dir / "instructions.md"

    def iteration_dir(self, n: int) -> Path:
        return self.iterations_dir / f"{n:04d}"

    # -- config / status -------------------------------------------------
    def load(self) -> dict:
        return json.loads((self.dir / "experiment.json").read_text())

    def save(self, doc: dict) -> None:
        with self._lock:
            tmp = self.dir / "experiment.json.tmp"
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.dir / "experiment.json")

    @property
    def config(self) -> ExperimentConfig:
        return ExperimentConfig.model_validate(self.load()["config"])

    def set_status(self, status: str, **extra) -> None:
        doc = self.load()
        doc["status"] = status
        doc.update(extra)
        doc["updated_at"] = time.time()
        self.save(doc)

    def instructions(self) -> str:
        return self.instructions_path.read_text()

    def set_instructions(self, text: str) -> None:
        self.instructions_path.write_text(text)

    # -- agent notebook -----------------------------------------------------
    # Persistent memory across iterations: survives rejected attempts, so the
    # agent never has to rediscover (or re-try) a failed idea.
    NOTEBOOK_WORKFILE = "AGENT_NOTES.md"

    @property
    def notebook_path(self) -> Path: return self.dir / "notebook.md"

    def notebook(self) -> str:
        return self.notebook_path.read_text() if self.notebook_path.exists() else ""

    def set_notebook(self, text: str) -> None:
        self.notebook_path.write_text(text)

    # -- champion ----------------------------------------------------------
    def champion_meta(self) -> dict:
        p = self.champion_dir / "champion.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def set_champion(self, iteration: int, metrics: dict, primary: float,
                     deleted_files: list[str]) -> None:
        self.champion_dir.mkdir(exist_ok=True)
        src = self.iteration_dir(iteration) / "files" if iteration > 0 else None
        if self.champion_files.exists():
            shutil.rmtree(self.champion_files)
        if src is not None and src.exists():
            shutil.copytree(src, self.champion_files)
        else:
            self.champion_files.mkdir(parents=True, exist_ok=True)
        (self.champion_dir / "champion.json").write_text(json.dumps({
            "iteration": iteration,
            "metrics": metrics,
            "primary": primary,
            "deleted_files": deleted_files,
            "updated_at": time.time(),
        }, indent=2))

    # -- history -----------------------------------------------------------
    def append_history(self, entry: dict) -> None:
        with self._lock:
            with open(self.dir / "history.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")

    def history(self) -> list[dict]:
        p = self.dir / "history.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    # -- iteration artifacts -------------------------------------------------
    def save_iteration(self, n: int, meta: dict, agent_log: str, summary: str,
                       diff: str, editable_files: list[str]) -> None:
        it = self.iteration_dir(n)
        it.mkdir(parents=True, exist_ok=True)
        (it / "meta.json").write_text(json.dumps(meta, indent=2))
        (it / "agent.log").write_text(agent_log)
        (it / "summary.md").write_text(summary)
        (it / "changes.diff").write_text(diff)
        files_dir = it / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir)
        files_dir.mkdir(parents=True)
        for rel in editable_files:
            src = self.work_dir / rel
            if src.exists():
                dst = files_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    def iteration_meta(self, n: int) -> dict:
        return json.loads((self.iteration_dir(n) / "meta.json").read_text())

    def iteration_artifact(self, n: int, name: str) -> str:
        p = self.iteration_dir(n) / name
        return p.read_text() if p.exists() else ""

    def editable_snapshot_list(self, cfg: ExperimentConfig, root: Path) -> list[str]:
        return [rel for rel in walk_files(root, cfg.ignore_patterns)
                if is_editable_path(rel, cfg.editable_files)]

    # -- export ------------------------------------------------------------
    def build_version_zip(self, n: int | None) -> bytes:
        """Full runnable workspace for iteration ``n`` (None = champion)."""
        cfg = self.config
        if n is None:
            files_dir = self.champion_files
            meta = self.champion_meta()
            deleted = set(meta.get("deleted_files", []))
        elif n == 0:
            files_dir, deleted = None, set()
        else:
            files_dir = self.iteration_dir(n) / "files"
            deleted = set(self.iteration_meta(n).get("deleted_editable", []))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            overlay = set(walk_files(files_dir, [])) if files_dir and files_dir.exists() else set()
            for rel in walk_files(self.baseline_dir, cfg.ignore_patterns):
                if rel in overlay or rel in deleted:
                    continue
                zf.write(self.baseline_dir / rel, rel)
            for rel in sorted(overlay):
                zf.write(files_dir / rel, rel)
        return buf.getvalue()


class ExperimentStore:
    def __init__(self, root: Path = DATA_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, config: ExperimentConfig, instructions: str) -> Experiment:
        config.guard_eval_not_editable()
        ws = Path(config.workspace)
        if not ws.is_dir():
            raise ValueError(f"workspace does not exist: {ws}")

        exp_id = f"{_slug(config.name)}-{uuid.uuid4().hex[:8]}"
        exp_dir = self.root / exp_id
        exp = Experiment(exp_dir)
        exp_dir.mkdir(parents=True)
        try:
            self._snapshot_baseline(ws, exp.baseline_dir, config.ignore_patterns)
            exp.instructions_path.write_text(instructions)
            exp.iterations_dir.mkdir()
            exp.save({
                "id": exp_id,
                "config": config.model_dump(),
                "status": "idle",
                "created_at": time.time(),
                "updated_at": time.time(),
            })
        except Exception:
            shutil.rmtree(exp_dir, ignore_errors=True)
            raise
        return exp

    @staticmethod
    def _snapshot_baseline(src: Path, dst: Path, ignore_patterns: list[str]) -> None:
        dst.mkdir(parents=True)
        for rel in walk_files(src, ignore_patterns):
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, target)

    def get(self, exp_id: str) -> Experiment:
        d = self.root / exp_id
        if not (d / "experiment.json").exists():
            raise KeyError(f"unknown experiment: {exp_id}")
        return Experiment(d)

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), reverse=True):
            f = d / "experiment.json"
            if f.exists():
                try:
                    doc = json.loads(f.read_text())
                except json.JSONDecodeError:
                    continue
                out.append({k: doc.get(k) for k in ("id", "status", "created_at", "updated_at")}
                           | {"name": doc.get("config", {}).get("name", d.name)})
        out.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
        return out

    def delete(self, exp_id: str) -> None:
        exp = self.get(exp_id)
        shutil.rmtree(exp.dir)
