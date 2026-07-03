"""Shared streaming subprocess runner for CLI-based agents."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ProcResult:
    returncode: int | None
    lines: list[str]
    timed_out: bool = False
    error: str | None = None


def run_streaming(cmd: list[str] | str, workdir: Path, timeout: int,
                  env: dict[str, str],
                  on_line: Callable[[str], None] | None = None,
                  shell: bool = False) -> ProcResult:
    """Run a command, streaming each stdout line to ``on_line`` as it arrives."""
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, env=env, shell=shell, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        return ProcResult(returncode=None, lines=[], error=str(exc))

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass  # a broken observer must never kill the agent

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(0.2)
    proc.wait()
    t.join(timeout=5)
    return ProcResult(returncode=proc.returncode, lines=lines, timed_out=timed_out)
