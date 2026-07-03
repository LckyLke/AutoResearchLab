"""Default agent: Claude Code CLI in headless mode (``claude -p``).

Uses ``--output-format stream-json`` so tool calls and text are parsed
live and surfaced to the GUI as they happen.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import AgentConfig
from .base import Agent, AgentResult, OnActivity, strip_ansi
from .proc import run_streaming


def _tool_activity(name: str, tool_input: dict) -> str:
    detail = (tool_input.get("file_path") or tool_input.get("path")
              or tool_input.get("command") or tool_input.get("pattern") or "")
    detail = str(detail)
    if len(detail) > 90:
        detail = detail[:90] + "…"
    return f"⚙ {name} {detail}".rstrip()


class _StreamParser:
    """Turns stream-json lines into readable log/activity lines."""

    def __init__(self, on_activity: OnActivity | None):
        self.on_activity = on_activity
        self.log_lines: list[str] = []
        self.final_result: str | None = None
        self.usage: dict | None = None

    def feed(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            self._emit(strip_ansi(raw))
            return
        etype = event.get("type")
        if etype == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self._emit(block["text"].strip())
                elif block.get("type") == "tool_use":
                    self._emit(_tool_activity(block.get("name", "tool"),
                                              block.get("input") or {}))
        elif etype == "result":
            self.final_result = event.get("result") or ""
            usage = event.get("usage") or {}
            self.usage = {
                "input_tokens": (usage.get("input_tokens", 0)
                                 + usage.get("cache_read_input_tokens", 0)
                                 + usage.get("cache_creation_input_tokens", 0)),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": event.get("total_cost_usd"),
            }
            if event.get("subtype") not in (None, "success"):
                self._emit(f"[result] {event.get('subtype')}")
        elif etype == "system" and event.get("subtype") == "init":
            model = event.get("model", "")
            self._emit(f"session started{f' · {model}' if model else ''}")

    def _emit(self, line: str) -> None:
        self.log_lines.append(line)
        if self.on_activity is not None:
            short = line.replace("\n", " ")
            self.on_activity(short[:160])


class ClaudeCodeAgent(Agent):
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [self.cfg.claude_binary, "-p", prompt,
               "--output-format", "stream-json", "--verbose"]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.skip_permissions:
            cmd += ["--dangerously-skip-permissions"]
        elif self.cfg.allowed_tools:
            cmd += ["--allowedTools", " ".join(self.cfg.allowed_tools)]
        cmd += list(self.cfg.extra_args)
        return cmd

    def run(self, prompt: str, workdir: Path, timeout_seconds: int,
            env: dict[str, str] | None = None,
            on_activity: OnActivity | None = None) -> AgentResult:
        # scrub outer-session state so a nested `claude -p` starts clean
        base = dict(env) if env is not None else os.environ.copy()
        run_env = {k: v for k, v in base.items()
                   if not k.startswith(("CLAUDE_", "CLAUDECODE"))}
        run_env["PYTHONDONTWRITEBYTECODE"] = "1"

        parser = _StreamParser(on_activity)
        result = run_streaming(self._build_command(prompt), workdir,
                               timeout_seconds, run_env, on_line=parser.feed)

        log = "\n".join(parser.log_lines)
        if result.error:
            return AgentResult(ok=False, log=log,
                               error=f"could not start claude: {result.error}")
        if result.timed_out:
            return AgentResult(ok=False, log=log,
                               error=f"agent timed out after {timeout_seconds}s "
                                     "(partial edits are still evaluated)")
        if result.returncode != 0:
            return AgentResult(ok=False, log=log,
                               error=f"claude exited with code {result.returncode}")
        summary = (parser.final_result or
                   (parser.log_lines[-1] if parser.log_lines else ""))
        return AgentResult(ok=True, summary=summary.strip()[-4000:], log=log,
                           usage=parser.usage)
