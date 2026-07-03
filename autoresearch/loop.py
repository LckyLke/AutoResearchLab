"""The research loop: propose → sandbox-check → evaluate → keep the champion.

Fully autonomous once started; the user only presses Start/Stop. Runs in a
daemon thread per experiment and reports progress through the EventBus.
"""

from __future__ import annotations

import difflib
import threading
import time
import traceback
from pathlib import Path

import json

from .agents import create_agent
from .config import ExperimentConfig
from .envs import (EnvironmentError_, ResolvedEnv, environment_prompt_block,
                   introspect_env, resolve_env)
from .evaluator import is_improvement, run_eval
from .events import EventBus
from .knowledge import KnowledgeStore, knowledge_prompt_block
from .prompts import build_prompt
from .sandbox import Sandbox, sync_workdir, walk_files, matches_any
from .storage import Experiment


class ResearchLoop:
    def __init__(self, experiment: Experiment, bus: EventBus):
        self.exp = experiment
        self.bus = bus
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.phase = "idle"  # idle | env | baseline | sync | agent | eval | decide
        self._env: ResolvedEnv | None = None
        self._env_block = ""
        self._last_activity_ts = 0.0

    # -- public ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("loop already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"loop-{self.exp.id}")
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()
        self._emit("stop_requested", {})

    # -- internals -----------------------------------------------------------

    def _emit(self, kind: str, payload: dict) -> None:
        self.bus.publish(self.exp.id, kind, payload)

    def _run(self) -> None:
        try:
            self.exp.set_status("running")
            self._emit("status", {"status": "running"})
            cfg = self.exp.config
            started = time.monotonic()

            self._prepare_environment(cfg)

            if not self.exp.champion_meta():
                self._run_baseline(cfg)

            while not self._should_stop(cfg, started):
                n = self._next_iteration_number()
                self._run_iteration(cfg, n)
        except Exception as exc:
            self.exp.set_status("error", error=str(exc))
            self._emit("error", {"message": str(exc), "trace": traceback.format_exc()})
        finally:
            self.phase = "idle"
            doc = self.exp.load()
            if doc.get("status") == "running":
                self.exp.set_status("stopped")
            self._emit("status", {"status": self.exp.load().get("status")})

    def _should_stop(self, cfg: ExperimentConfig, started: float) -> bool:
        if self._stop.is_set():
            self.exp.set_status("stopped")
            return True
        b = cfg.budgets
        done = self._next_iteration_number() - 1
        if b.max_iterations is not None and done >= b.max_iterations:
            self.exp.set_status("finished", reason="max_iterations reached")
            return True
        if b.max_runtime_seconds is not None and time.monotonic() - started >= b.max_runtime_seconds:
            self.exp.set_status("finished", reason="time budget exhausted")
            return True
        if b.max_cost_usd is not None:
            spent = sum((h.get("usage") or {}).get("cost_usd") or 0.0
                        for h in self.exp.history())
            if spent >= b.max_cost_usd:
                self.exp.set_status("finished",
                                    reason=f"cost budget exhausted (${spent:.2f})")
                return True
        return False

    def _next_iteration_number(self) -> int:
        existing = [int(p.name) for p in self.exp.iterations_dir.iterdir()
                    if p.is_dir() and p.name.isdigit()]
        return (max(existing) + 1) if existing else 1

    # -- environment -----------------------------------------------------------

    def _prepare_environment(self, cfg: ExperimentConfig) -> None:
        self.phase = "env"
        self._emit("phase", {"phase": "env"})
        try:
            resolved = resolve_env(cfg.environment)
        except EnvironmentError_ as exc:
            raise RuntimeError(f"environment error: {exc}") from exc

        self._env = resolved
        if not cfg.environment.inform_agent:
            self._env_block = ""  # introspection only exists to inform the agent
            self._emit("environment", {"kind": resolved.kind, "name": resolved.name})
            return

        cache_path = self.exp.dir / "environment.json"
        key = cfg.environment.model_dump_json()
        info = None
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text())
                if cached.get("key") == key:
                    info = cached.get("info")
            except (json.JSONDecodeError, OSError):
                pass
        if info is None:
            info = introspect_env(resolved)
            cache_path.write_text(json.dumps({"key": key, "info": info}, indent=2))

        self._env_block = environment_prompt_block(info, resolved)
        self._emit("environment", {
            "kind": resolved.kind, "name": resolved.name,
            "python_version": info.get("python_version"),
            "package_count": len(info.get("packages") or []),
        })
        return

    def _exec_env(self) -> dict[str, str]:
        return self._env.apply() if self._env else None

    # -- baseline --------------------------------------------------------------

    def _run_holdout(self, cfg: ExperimentConfig) -> dict | None:
        """Run the hidden anti-overfitting eval. Its result is stored for the
        user but NEVER fed back into the agent prompt."""
        if not cfg.eval.holdout_command.strip():
            return None
        self._emit("phase", {"phase": "holdout"})
        holdout_cfg = cfg.eval.model_copy(update={"command": cfg.eval.holdout_command})
        result = run_eval(holdout_cfg, self.exp.work_dir, base_env=self._exec_env())
        return {
            "ok": result.ok,
            "primary": result.primary,
            "metrics": result.metrics,
            "error": result.error,
        }

    def _run_baseline(self, cfg: ExperimentConfig) -> None:
        self.phase = "baseline"
        self._emit("phase", {"phase": "baseline"})
        sync_workdir(self.exp.baseline_dir, None, self.exp.work_dir, cfg.ignore_patterns)
        result = run_eval(cfg.eval, self.exp.work_dir, base_env=self._exec_env())
        primary = result.primary if result.ok else None
        holdout = self._run_holdout(cfg)
        self.exp.set_champion(0, result.metrics, primary, deleted_files=[])
        entry = {
            "iteration": 0,
            "ts": time.time(),
            "eval_ok": result.ok,
            "primary": primary,
            "metrics": result.metrics,
            "is_champion": True,
            "summary": "Baseline: the workspace exactly as provided.",
            "eval_error": result.error,
            "duration_seconds": result.duration_seconds,
            "holdout": holdout,
        }
        self.exp.append_history(entry)
        self._emit("iteration", entry)

    # -- one iteration -----------------------------------------------------------

    def _run_iteration(self, cfg: ExperimentConfig, n: int) -> None:
        t0 = time.monotonic()
        self._emit("iteration_started", {"iteration": n})
        champion = self.exp.champion_meta()
        champ_primary = champion.get("primary")
        champ_deleted = champion.get("deleted_files", [])

        # 1. reset working copy to the champion state
        self.phase = "sync"
        self._emit("phase", {"phase": "sync", "iteration": n})
        sync_workdir(self.exp.baseline_dir, self.exp.champion_files,
                     self.exp.work_dir, cfg.ignore_patterns, champ_deleted)
        pre_files = {rel: (self.exp.work_dir / rel).read_text(errors="replace")
                     for rel in self.exp.editable_snapshot_list(cfg, self.exp.work_dir)}

        # persistent notebook: inject the agent's memory into the workdir
        notebook_workfile = self.exp.work_dir / self.exp.NOTEBOOK_WORKFILE
        notebook_workfile.write_text(self.exp.notebook())

        # 2. let the agent work, with the sandbox armed
        self.phase = "agent"
        self._emit("phase", {"phase": "agent", "iteration": n})
        prompt = build_prompt(cfg, self.exp.instructions(), n, champ_primary,
                              champion.get("metrics", {}), self.exp.history(),
                              environment_block=self._env_block,
                              knowledge_block=knowledge_prompt_block(KnowledgeStore(self.exp.dir)),
                              notebook=self.exp.notebook())
        sandbox = Sandbox(self.exp.work_dir, self.exp.baseline_dir,
                          cfg.editable_files + [self.exp.NOTEBOOK_WORKFILE],
                          cfg.ignore_patterns)
        sandbox.protect()

        def on_activity(line: str) -> None:
            now = time.monotonic()
            if now - self._last_activity_ts < 0.35:  # keep SSE traffic sane
                return
            self._last_activity_ts = now
            self._emit("agent_activity", {
                "iteration": n, "line": line,
                "elapsed": round(now - t0, 1),
            })

        try:
            agent_result = create_agent(cfg.agent).run(
                prompt, self.exp.work_dir, cfg.budgets.agent_timeout_seconds,
                env=self._exec_env(), on_activity=on_activity)
        finally:
            sandbox.unprotect()
        report = sandbox.enforce()

        # persist the notebook whatever happens to the code change
        if notebook_workfile.exists():
            self.exp.set_notebook(notebook_workfile.read_text())

        # 3. diff against the champion
        diff = self._diff(cfg, pre_files)

        # 4. evaluate
        self.phase = "eval"
        self._emit("phase", {"phase": "eval", "iteration": n})
        eval_result = run_eval(cfg.eval, self.exp.work_dir, base_env=self._exec_env())

        # 5. champion decision — the eval is the only judge; even a timed-out
        # agent's partial edit is kept if it scores better
        improved = eval_result.ok and is_improvement(
            eval_result.primary, champ_primary, cfg.eval.direction, cfg.budgets.min_improvement)

        # 6. anti-overfitting check: holdout runs only for new champions,
        # and its outcome is never shown to the agent
        holdout = self._run_holdout(cfg) if improved else None

        deleted_now = sorted((set(champ_deleted) | set(report.deleted_editable))
                             - {self.exp.NOTEBOOK_WORKFILE})
        meta = {
            "iteration": n,
            "ts": time.time(),
            "agent_ok": agent_result.ok,
            "agent_error": agent_result.error,
            "eval_ok": eval_result.ok,
            "eval": eval_result.to_dict(),
            "primary": eval_result.primary,
            "metrics": eval_result.metrics,
            "is_champion": bool(improved),
            "champion_before": champ_primary,
            "sandbox": report.to_dict(),
            "deleted_editable": deleted_now,
            "duration_seconds": round(time.monotonic() - t0, 3),
            "summary": agent_result.summary,
            "usage": agent_result.usage,
            "holdout": holdout,
        }
        editable_now = self.exp.editable_snapshot_list(cfg, self.exp.work_dir)
        self.exp.save_iteration(n, meta, agent_result.log or "",
                                agent_result.summary or "", diff, editable_now)
        if improved:
            self.exp.set_champion(n, eval_result.metrics, eval_result.primary, deleted_now)

        entry = {
            "iteration": n,
            "ts": meta["ts"],
            "eval_ok": eval_result.ok,
            "primary": eval_result.primary,
            "metrics": eval_result.metrics,
            "is_champion": bool(improved),
            "summary": (agent_result.summary or "")[:600],
            "agent_error": agent_result.error,
            "eval_error": eval_result.error,
            "violations": len(report.violations),
            "duration_seconds": meta["duration_seconds"],
            "usage": agent_result.usage,
            "holdout": holdout,
        }
        self.exp.append_history(entry)
        self._emit("iteration", entry)

    def _diff(self, cfg: ExperimentConfig, pre_files: dict[str, str]) -> str:
        chunks: list[str] = []
        post_paths = set(self.exp.editable_snapshot_list(cfg, self.exp.work_dir))
        for rel in sorted(set(pre_files) | post_paths):
            before = pre_files.get(rel, "")
            after = ((self.exp.work_dir / rel).read_text(errors="replace")
                     if rel in post_paths else "")
            if before == after:
                continue
            chunks.extend(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}"))
        return "".join(chunks)


class LoopManager:
    """One ResearchLoop per experiment, shared EventBus."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._loops: dict[str, ResearchLoop] = {}
        self._lock = threading.Lock()

    def get(self, experiment: Experiment) -> ResearchLoop:
        with self._lock:
            loop = self._loops.get(experiment.id)
            if loop is None:
                loop = ResearchLoop(experiment, self.bus)
                self._loops[experiment.id] = loop
            return loop

    def status(self, experiment_id: str) -> dict:
        loop = self._loops.get(experiment_id)
        return {
            "running": bool(loop and loop.running),
            "phase": loop.phase if loop else "idle",
        }
