"""Run the user-supplied eval command and collect metrics.

Contract: the eval command runs with the workspace as cwd and, on
success, writes a JSON object of metrics to ``metrics_file`` (default
``metrics.json``). If the file is missing, the last stdout line that
parses as a JSON object is accepted as a fallback.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import EvalConfig

_TAIL = 20_000  # keep logs bounded


@dataclass
class EvalResult:
    ok: bool
    metrics: dict = field(default_factory=dict)
    primary: float | None = None
    returncode: int | None = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "metrics": self.metrics,
            "primary": self.primary,
            "returncode": self.returncode,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
        }


def _extract_stdout_json(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def run_eval(cfg: EvalConfig, workdir: Path,
             base_env: dict[str, str] | None = None) -> EvalResult:
    metrics_path = workdir / cfg.metrics_file
    if metrics_path.exists():
        metrics_path.unlink()

    start = time.monotonic()
    env = dict(base_env) if base_env is not None else os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # stale .pyc must never grade a fresh source
    try:
        proc = subprocess.run(
            cfg.command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return EvalResult(
            ok=False,
            duration_seconds=time.monotonic() - start,
            stdout=(exc.stdout or b"").decode(errors="replace")[-_TAIL:] if isinstance(exc.stdout, bytes) else (exc.stdout or "")[-_TAIL:],
            stderr=(exc.stderr or b"").decode(errors="replace")[-_TAIL:] if isinstance(exc.stderr, bytes) else (exc.stderr or "")[-_TAIL:],
            error=f"eval timed out after {cfg.timeout_seconds}s",
        )

    duration = time.monotonic() - start
    result = EvalResult(
        ok=False,
        returncode=proc.returncode,
        duration_seconds=duration,
        stdout=proc.stdout[-_TAIL:],
        stderr=proc.stderr[-_TAIL:],
    )

    if proc.returncode != 0:
        result.error = f"eval exited with code {proc.returncode}"
        return result

    metrics: dict | None = None
    if metrics_path.exists():
        try:
            loaded = json.loads(metrics_path.read_text())
            if isinstance(loaded, dict):
                metrics = loaded
        except (json.JSONDecodeError, OSError) as exc:
            result.error = f"could not parse {cfg.metrics_file}: {exc}"
            return result
    if metrics is None:
        metrics = _extract_stdout_json(proc.stdout)
    if metrics is None:
        result.error = f"eval produced no {cfg.metrics_file} and no JSON on stdout"
        return result

    if cfg.metric not in metrics:
        result.error = f"metric '{cfg.metric}' missing from eval output (got: {sorted(metrics)})"
        result.metrics = metrics
        return result

    try:
        primary = float(metrics[cfg.metric])
    except (TypeError, ValueError):
        result.error = f"metric '{cfg.metric}' is not numeric: {metrics[cfg.metric]!r}"
        result.metrics = metrics
        return result

    result.ok = True
    result.metrics = metrics
    result.primary = primary
    return result


def is_improvement(candidate: float, champion: float | None,
                   direction: str, min_improvement: float = 0.0) -> bool:
    if champion is None:
        return True
    if direction == "maximize":
        return candidate > champion + min_improvement
    return candidate < champion - min_improvement
