"""Generic CLI agent: run any command as the "agent".

``command_template`` supports two placeholders (literal replacement, so
braces in your command are safe):
  {prompt_file} — path to a temp file containing the full prompt
  {workdir}     — the working directory

Examples:
  aider --yes --message-file {prompt_file}
  python my_agent.py --task {prompt_file}
This also makes the whole loop testable without a model.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..config import AgentConfig
from .base import Agent, AgentResult, OnActivity, strip_ansi
from .proc import run_streaming


class CommandAgent(Agent):
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def run(self, prompt: str, workdir: Path, timeout_seconds: int,
            env: dict[str, str] | None = None,
            on_activity: OnActivity | None = None) -> AgentResult:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        cmd = (self.cfg.command_template
               .replace("{prompt_file}", prompt_file)
               .replace("{workdir}", str(workdir)))
        run_env = dict(env) if env is not None else os.environ.copy()
        run_env["PYTHONDONTWRITEBYTECODE"] = "1"

        def on_line(line: str) -> None:
            if on_activity is not None and line.strip():
                on_activity(strip_ansi(line.strip())[:160])

        result = run_streaming(cmd, workdir, timeout_seconds, run_env,
                               on_line=on_line, shell=True)
        log = strip_ansi("\n".join(result.lines))
        if result.error:
            return AgentResult(ok=False, log=log, error=result.error)
        if result.timed_out:
            return AgentResult(ok=False, log=log,
                               error=f"agent command timed out after {timeout_seconds}s")
        if result.returncode != 0:
            return AgentResult(ok=False, log=log,
                               error=f"agent command exited with code {result.returncode}")
        return AgentResult(ok=True, summary=log.strip()[-4000:], log=log)
