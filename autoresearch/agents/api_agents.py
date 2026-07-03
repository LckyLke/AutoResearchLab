"""API-backed agents: a small file-editing tool loop over raw HTTP.

Two providers share the same four workspace tools:

* ``AnthropicAPIAgent``  — Anthropic Messages API (default model claude-opus-4-8)
* ``OpenAICompatAgent``  — any OpenAI-compatible /chat/completions endpoint
  (Ollama, llama.cpp server, vLLM, LM Studio, OpenAI itself, ...)

Tool file access is confined to the working directory; the outer sandbox
additionally reverts any change outside the editable whitelist.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from ..config import AgentConfig
from .base import Agent, AgentResult

TOOL_SPECS = [
    {
        "name": "list_files",
        "description": "List files in the working directory (recursively), with sizes.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_file",
        "description": "Read a text file. Path is relative to the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file with the given content. "
                       "Path is relative to the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the working directory (e.g. the eval "
                       "command to check your work). Returns stdout+stderr, truncated.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

_MAX_TOOL_OUTPUT = 24_000
_MAX_FILE_READ = 200_000


class _WorkspaceTools:
    def __init__(self, workdir: Path, allow_run_command: bool, deadline: float,
                 env: dict[str, str] | None = None):
        self.workdir = workdir.resolve()
        self.allow_run_command = allow_run_command
        self.deadline = deadline
        self.env = dict(env) if env is not None else os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def _resolve(self, rel: str) -> Path:
        p = (self.workdir / rel).resolve()
        if not p.is_relative_to(self.workdir):
            raise PermissionError(f"path escapes the working directory: {rel}")
        return p

    def call(self, name: str, args: dict) -> str:
        try:
            if name == "list_files":
                lines = []
                for p in sorted(self.workdir.rglob("*")):
                    if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
                        lines.append(f"{p.relative_to(self.workdir)}  ({p.stat().st_size} bytes)")
                return "\n".join(lines) or "(empty)"
            if name == "read_file":
                p = self._resolve(args["path"])
                text = p.read_text(errors="replace")
                if len(text) > _MAX_FILE_READ:
                    text = text[:_MAX_FILE_READ] + "\n... [truncated]"
                return text
            if name == "write_file":
                p = self._resolve(args["path"])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(args["content"])
                return f"wrote {len(args['content'])} chars to {args['path']}"
            if name == "run_command":
                if not self.allow_run_command:
                    return "error: run_command is disabled for this experiment"
                budget = max(5, min(600, int(self.deadline - time.monotonic()) - 10))
                proc = subprocess.run(args["command"], shell=True, cwd=self.workdir,
                                      capture_output=True, text=True, timeout=budget,
                                      env=self.env)
                out = (proc.stdout + "\n" + proc.stderr).strip()
                if len(out) > _MAX_TOOL_OUTPUT:
                    out = out[:_MAX_TOOL_OUTPUT] + "\n... [truncated]"
                return f"exit code {proc.returncode}\n{out}"
            return f"error: unknown tool {name}"
        except subprocess.TimeoutExpired:
            return "error: command timed out"
        except Exception as exc:  # tool errors go back to the model, not up the stack
            return f"error: {exc}"


class _ToolLoopAgent(Agent):
    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self._tokens = {"input_tokens": 0, "output_tokens": 0}

    def _count_usage(self, input_tokens: int, output_tokens: int) -> None:
        self._tokens["input_tokens"] += int(input_tokens or 0)
        self._tokens["output_tokens"] += int(output_tokens or 0)

    def run(self, prompt: str, workdir: Path, timeout_seconds: int,
            env: dict[str, str] | None = None,
            on_activity=None) -> AgentResult:
        deadline = time.monotonic() + timeout_seconds
        tools = _WorkspaceTools(workdir, self.cfg.allow_run_command, deadline, env)
        log_parts: list[str] = []
        final_text = ""

        def note(line: str) -> None:
            log_parts.append(line)
            if on_activity is not None:
                try:
                    on_activity(line.replace("\n", " ")[:160])
                except Exception:
                    pass

        try:
            messages = self._initial_messages(prompt)
            for turn in range(self.cfg.max_tool_turns):
                remaining = deadline - time.monotonic()
                if remaining < 15:
                    note("[loop] stopping: time budget exhausted")
                    break
                text, tool_calls = self._step(messages, remaining, log_parts)
                if text.strip():
                    final_text = text.strip()
                    note(f"[assistant] {text.strip()}")
                if not tool_calls:
                    break
                for call_id, name, args in tool_calls:
                    note(f"⚙ {name}({json.dumps(args)[:300]})")
                    result = tools.call(name, args)
                    log_parts.append(f"[tool result] {result[:800]}")
                    self._append_tool_result(messages, call_id, result)
            else:
                note("[loop] stopping: max tool turns reached")
        except Exception as exc:
            return AgentResult(ok=False, log="\n".join(log_parts), error=str(exc),
                               usage=self._usage_or_none())
        return AgentResult(ok=True, summary=final_text[-4000:], log="\n".join(log_parts),
                           usage=self._usage_or_none())

    def _usage_or_none(self) -> dict | None:
        if not any(self._tokens.values()):
            return None
        return {**self._tokens, "cost_usd": None}

    # provider-specific hooks --------------------------------------------
    def _initial_messages(self, prompt: str) -> list[dict]:
        raise NotImplementedError

    def _step(self, messages: list[dict], remaining: float,
              log: list[str]) -> tuple[str, list[tuple[str, str, dict]]]:
        """One model request. Appends the assistant turn to ``messages`` and
        returns (text, [(tool_call_id, tool_name, args), ...])."""
        raise NotImplementedError

    def _append_tool_result(self, messages: list[dict], call_id: str, result: str) -> None:
        raise NotImplementedError


class AnthropicAPIAgent(_ToolLoopAgent):
    API_URL = "https://api.anthropic.com/v1/messages"

    def _api_key(self) -> str:
        env = self.cfg.api_key_env or "ANTHROPIC_API_KEY"
        key = os.environ.get(env, "")
        if not key:
            raise RuntimeError(f"no API key found in ${env}")
        return key

    def _initial_messages(self, prompt: str) -> list[dict]:
        return [{"role": "user", "content": prompt}]

    def _step(self, messages, remaining, log):
        body = {
            "model": self.cfg.model or "claude-opus-4-8",
            "max_tokens": self.cfg.max_output_tokens,
            "thinking": {"type": "adaptive"},
            "tools": TOOL_SPECS,
            "messages": messages,
        }
        resp = httpx.post(
            self.API_URL,
            headers={
                "x-api-key": self._api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=min(remaining, 600),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        usage = data.get("usage") or {}
        self._count_usage(
            usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            usage.get("output_tokens", 0))
        if data.get("stop_reason") == "refusal":
            raise RuntimeError("the model refused the request (stop_reason=refusal)")
        content = data.get("content", [])
        # echo the assistant content back verbatim (required for thinking blocks)
        messages.append({"role": "assistant", "content": content})
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = [(b["id"], b["name"], b.get("input") or {})
                 for b in content if b.get("type") == "tool_use"]
        return text, calls

    def _append_tool_result(self, messages, call_id, result):
        # all tool results for one assistant turn belong in ONE user message
        if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
            messages[-1]["content"].append(
                {"type": "tool_result", "tool_use_id": call_id, "content": result})
        else:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": result}]})


class OpenAICompatAgent(_ToolLoopAgent):
    def _base_url(self) -> str:
        return (self.cfg.api_base_url or "http://localhost:11434/v1").rstrip("/")

    def _initial_messages(self, prompt: str) -> list[dict]:
        return [{"role": "user", "content": prompt}]

    def _step(self, messages, remaining, log):
        headers = {"content-type": "application/json"}
        env = self.cfg.api_key_env or "OPENAI_API_KEY"
        key = os.environ.get(env, "")
        if key:
            headers["authorization"] = f"Bearer {key}"
        body = {
            "model": self.cfg.model or "llama3.1",
            "messages": messages,
            "tools": [{"type": "function",
                       "function": {"name": t["name"], "description": t["description"],
                                    "parameters": t["input_schema"]}}
                      for t in TOOL_SPECS],
        }
        resp = httpx.post(f"{self._base_url()}/chat/completions", headers=headers,
                          json=body, timeout=min(remaining, 600))
        if resp.status_code != 200:
            raise RuntimeError(f"API error {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        usage = payload.get("usage") or {}
        self._count_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        msg = payload["choices"][0]["message"]
        messages.append(msg)
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((tc["id"], tc["function"]["name"], args))
        return msg.get("content") or "", calls

    def _append_tool_result(self, messages, call_id, result):
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
