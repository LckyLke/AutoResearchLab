"""FastAPI application: REST + SSE + static GUI."""

from __future__ import annotations

import asyncio
import io
import queue
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import EnvironmentConfig, ExperimentConfig
from .demo import ensure_demo_experiment
from .envs import EnvironmentError_, introspect_env, list_conda_envs, resolve_env
from .events import EventBus
from .knowledge import KnowledgeStore
from .loop import LoopManager
from .sandbox import walk_files
from .storage import DATA_DIR, ExperimentStore

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
TEMPLATES_DIR = ROOT / "templates"

bus = EventBus()
store = ExperimentStore()
loops = LoopManager(bus)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        created = ensure_demo_experiment(store)
        if created:
            print(f"seeded demo experiment: {created}")
    except Exception as exc:  # a broken demo must never block the server
        print(f"could not seed demo experiment: {exc}")
    yield


app = FastAPI(title="AutoResearch", version="0.1.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# -- filesystem browsing (for the setup wizard) -----------------------------

class BrowseRequest(BaseModel):
    path: str = "~"


@app.post("/api/browse")
def browse(req: BrowseRequest):
    p = Path(req.path).expanduser()
    try:
        p = p.resolve()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {p}")
        skip_dirs = {".git", "__pycache__", "node_modules", ".cache", ".pytest_cache"}
        dirs, files = [], []
        for child in sorted(p.iterdir(), key=lambda c: (c.name.startswith("."), c.name.lower())):
            if child.is_dir():
                if child.name not in skip_dirs:
                    dirs.append(child.name)  # dot-dirs stay visible (.venv!)
            elif not child.name.startswith("."):
                files.append(child.name)
    except PermissionError:
        raise HTTPException(403, f"permission denied: {p}")
    return {"path": str(p), "parent": str(p.parent), "dirs": dirs, "files": files}


MAX_TREE_FILES = 15_000


class TreeRequest(BaseModel):
    workspace: str
    extra_ignore: list[str] = []


@app.post("/api/tree")
def tree(req: TreeRequest):
    from .config import DEFAULT_IGNORE_PATTERNS
    ws = Path(req.workspace).expanduser()
    if not ws.is_dir():
        raise HTTPException(400, f"not a directory: {ws}")
    ignore = DEFAULT_IGNORE_PATTERNS + [p.strip() for p in req.extra_ignore if p.strip()]
    files = walk_files(ws, ignore)
    if len(files) > MAX_TREE_FILES:
        by_top: dict[str, int] = {}
        for rel in files:
            top = rel.split("/", 1)[0] if "/" in rel else "(root)"
            by_top[top] = by_top.get(top, 0) + 1
        heaviest = ", ".join(f"{name}/ ({n})" for name, n in
                             sorted(by_top.items(), key=lambda kv: -kv[1])[:5])
        raise HTTPException(400, f"workspace has {len(files)} files (after ignores) — "
                                 f"heaviest: {heaviest}. Add ignore patterns for the "
                                 "bulky folders, or pick a smaller folder.")
    return {"workspace": str(ws.resolve()), "files": files,
            "ignore_defaults": DEFAULT_IGNORE_PATTERNS}


# -- python environments -----------------------------------------------------

@app.get("/api/env/conda")
def conda_envs():
    return {"envs": list_conda_envs()}


@app.post("/api/env/check")
def check_env(cfg: EnvironmentConfig):
    """Resolve + introspect an environment so the wizard can verify it."""
    try:
        resolved = resolve_env(cfg)
    except EnvironmentError_ as exc:
        raise HTTPException(400, str(exc))
    info = introspect_env(resolved)
    if info.get("error") or not info.get("python_version"):
        raise HTTPException(400, f"environment found at {resolved.prefix or 'PATH'} "
                                 f"but python check failed: {info.get('error', 'no python')}")
    return {
        "kind": resolved.kind,
        "name": resolved.name,
        "prefix": resolved.prefix,
        "python_version": info["python_version"],
        "package_count": len(info["packages"]),
        "packages_sample": info["packages"][:12],
    }


# -- instruction templates ---------------------------------------------------

@app.get("/api/templates")
def templates():
    return {
        "default": (TEMPLATES_DIR / "INSTRUCTIONS.default.md").read_text(),
        "template": (TEMPLATES_DIR / "INSTRUCTIONS.template.md").read_text(),
    }


# -- experiments --------------------------------------------------------------

class CreateExperimentRequest(BaseModel):
    config: ExperimentConfig
    instructions: str = ""


@app.get("/api/experiments")
def list_experiments():
    return {"experiments": store.list(), "data_dir": str(DATA_DIR)}


@app.post("/api/experiments")
def create_experiment(req: CreateExperimentRequest):
    instructions = req.instructions
    if not instructions.strip():
        instructions = (TEMPLATES_DIR / "INSTRUCTIONS.default.md").read_text()
    try:
        exp = store.create(req.config, instructions)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc))
    return {"id": exp.id}


def _get_exp(exp_id: str):
    try:
        return store.get(exp_id)
    except KeyError:
        raise HTTPException(404, f"unknown experiment: {exp_id}")


@app.get("/api/experiments/{exp_id}")
def get_experiment(exp_id: str):
    exp = _get_exp(exp_id)
    doc = exp.load()
    return {
        **doc,
        "instructions": exp.instructions(),
        "history": exp.history(),
        "champion": exp.champion_meta(),
        "knowledge": KnowledgeStore(exp.dir).list(),
        "notebook": exp.notebook(),
        "loop": loops.status(exp_id),
    }


@app.delete("/api/experiments/{exp_id}")
def delete_experiment(exp_id: str):
    exp = _get_exp(exp_id)
    if loops.status(exp_id)["running"]:
        raise HTTPException(409, "stop the experiment before deleting it")
    store.delete(exp_id)
    return {"ok": True}


class InstructionsRequest(BaseModel):
    instructions: str


@app.put("/api/experiments/{exp_id}/instructions")
def update_instructions(exp_id: str, req: InstructionsRequest):
    exp = _get_exp(exp_id)
    exp.set_instructions(req.instructions)
    bus.publish(exp_id, "instructions_updated", {})
    return {"ok": True}


class NotebookRequest(BaseModel):
    notebook: str


@app.put("/api/experiments/{exp_id}/notebook")
def update_notebook(exp_id: str, req: NotebookRequest):
    exp = _get_exp(exp_id)
    exp.set_notebook(req.notebook)
    bus.publish(exp_id, "notebook_updated", {})
    return {"ok": True}


@app.post("/api/experiments/{exp_id}/start")
def start_experiment(exp_id: str):
    exp = _get_exp(exp_id)
    loop = loops.get(exp)
    if loop.running:
        raise HTTPException(409, "already running")
    loop.start()
    return {"ok": True}


@app.post("/api/experiments/{exp_id}/stop")
def stop_experiment(exp_id: str):
    exp = _get_exp(exp_id)
    loop = loops.get(exp)
    if not loop.running:
        raise HTTPException(409, "not running")
    loop.request_stop()
    return {"ok": True}


# -- knowledge library ---------------------------------------------------------

@app.get("/api/experiments/{exp_id}/knowledge")
def list_knowledge(exp_id: str):
    exp = _get_exp(exp_id)
    return {"documents": KnowledgeStore(exp.dir).list()}


@app.post("/api/experiments/{exp_id}/knowledge")
def upload_knowledge(exp_id: str, file: UploadFile):
    # sync on purpose: PDF extraction/OCR can take a while and must run in
    # the threadpool, not on the event loop
    exp = _get_exp(exp_id)
    data = file.file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        entry = KnowledgeStore(exp.dir).add(file.filename or "document", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    bus.publish(exp_id, "knowledge_updated", {"added": entry["name"]})
    return {"document": entry}


@app.delete("/api/experiments/{exp_id}/knowledge/{name}")
def delete_knowledge(exp_id: str, name: str):
    exp = _get_exp(exp_id)
    try:
        KnowledgeStore(exp.dir).remove(name)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    bus.publish(exp_id, "knowledge_updated", {"removed": name})
    return {"ok": True}


# -- iteration artifacts -----------------------------------------------------

@app.get("/api/experiments/{exp_id}/iterations/{n}")
def iteration_detail(exp_id: str, n: int):
    exp = _get_exp(exp_id)
    it = exp.iteration_dir(n)
    if not it.is_dir():
        raise HTTPException(404, f"no iteration {n}")
    return {
        "meta": exp.iteration_meta(n),
        "diff": exp.iteration_artifact(n, "changes.diff"),
        "agent_log": exp.iteration_artifact(n, "agent.log"),
        "summary": exp.iteration_artifact(n, "summary.md"),
    }


@app.get("/api/experiments/{exp_id}/iterations/{n}/download")
def download_iteration(exp_id: str, n: int):
    exp = _get_exp(exp_id)
    if n != 0 and not exp.iteration_dir(n).is_dir():
        raise HTTPException(404, f"no iteration {n}")
    data = exp.build_version_zip(n)
    return StreamingResponse(
        io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{exp_id}-v{n}.zip"'})


@app.get("/api/experiments/{exp_id}/champion/download")
def download_champion(exp_id: str):
    exp = _get_exp(exp_id)
    if not exp.champion_meta():
        raise HTTPException(404, "no champion yet")
    data = exp.build_version_zip(None)
    return StreamingResponse(
        io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{exp_id}-champion.zip"'})


# -- live events (SSE) --------------------------------------------------------

@app.get("/api/experiments/{exp_id}/events")
async def events(exp_id: str):
    _get_exp(exp_id)

    async def stream():
        q = bus.subscribe()
        try:
            yield "data: {\"kind\": \"connected\"}\n\n"
            while True:
                try:
                    event = await asyncio.to_thread(q.get, True, 15.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if event["experiment_id"] == exp_id:
                    yield EventBus.sse_format(event)
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
