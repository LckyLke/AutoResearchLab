"""Configuration models for experiments.

An experiment is fully described by an :class:`ExperimentConfig`. It is
validated once at creation time and stored as JSON inside the experiment
directory, so a run is always reproducible from disk.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Slash-less entries match that name as any path segment, at ANY depth
# (see sandbox.ignore_match) — so nested frontend/node_modules etc. are
# pruned too. Patterns with a slash are rooted at the workspace.
DEFAULT_IGNORE_PATTERNS = [
    ".git", ".hg", ".svn",
    "__pycache__", "*.pyc", "*.pyo",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".venv", "venv", ".virtualenv",
    "node_modules", ".next", ".nuxt", ".turbo", ".parcel-cache",
    ".cache", ".idea", ".vscode", ".DS_Store",
    ".ipynb_checkpoints",
    "metrics.json",
    ".scratch",  # sanctioned agent scratch space: never snapshotted, wiped each iteration
]

AgentType = Literal["claude_code", "anthropic_api", "openai_compat", "command"]


class EnvironmentConfig(BaseModel):
    """Which Python environment eval + agent commands run in."""

    type: Literal["system", "conda", "venv"] = "system"
    conda_env: str = ""   # env name or absolute prefix path
    venv_path: str = ""   # path to a virtualenv directory
    inform_agent: bool = True  # include python version + packages in the prompt


class AgentConfig(BaseModel):
    """Which agent edits the code and how it is invoked."""

    type: AgentType = "claude_code"
    model: Optional[str] = None

    # claude_code
    claude_binary: str = "claude"
    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "Bash"]
    )
    skip_permissions: bool = False
    extra_args: list[str] = Field(default_factory=list)

    # anthropic_api / openai_compat
    api_base_url: Optional[str] = None  # openai_compat: e.g. http://localhost:11434/v1
    api_key_env: Optional[str] = None  # env var holding the API key
    max_tool_turns: int = 40
    max_output_tokens: int = 16000
    allow_run_command: bool = True  # expose a run_command tool to API agents

    # command: any CLI, e.g. "aider --message {prompt_file}" — general escape hatch
    command_template: str = ""

    @model_validator(mode="after")
    def _check(self) -> "AgentConfig":
        if self.type == "command" and not self.command_template.strip():
            raise ValueError("agent.command_template is required for agent type 'command'")
        return self


class EvalConfig(BaseModel):
    """How a candidate solution is scored."""

    command: str  # executed with the workspace as cwd, e.g. "python eval.py"
    metrics_file: str = "metrics.json"  # written by the eval command, workspace-relative
    metric: str  # primary metric key inside the metrics file
    direction: Literal["maximize", "minimize"] = "maximize"
    timeout_seconds: int = Field(default=600, ge=1)
    # Optional anti-overfitting guard: a second eval (different seed/split)
    # run ONLY when a new champion is crowned. Its score is shown to the
    # user but never to the agent, so the agent cannot optimize against it.
    holdout_command: str = ""

    @field_validator("command")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("eval.command must not be empty")
        return v


class BudgetConfig(BaseModel):
    """Stop conditions. With everything unset the loop runs until stopped."""

    agent_timeout_seconds: int = Field(default=1800, ge=10)
    max_iterations: Optional[int] = Field(default=None, ge=1)
    max_runtime_seconds: Optional[int] = Field(default=None, ge=1)
    max_cost_usd: Optional[float] = Field(default=None, gt=0)  # cumulative agent spend
    min_improvement: float = 0.0  # required margin over the champion metric


class ExperimentConfig(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    workspace: str  # absolute path to the user's project folder
    editable_files: list[str]  # workspace-relative paths or glob patterns
    eval: EvalConfig
    agent: AgentConfig = Field(default_factory=AgentConfig)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    ignore_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS))

    @field_validator("editable_files")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        v = [p.strip().replace("\\", "/") for p in v if p.strip()]
        if not v:
            raise ValueError("select at least one editable file")
        for p in v:
            if p.startswith("/") or p.startswith(".."):
                raise ValueError(f"editable path must be workspace-relative: {p}")
        return v

    @model_validator(mode="after")
    def _validate_workspace(self) -> "ExperimentConfig":
        ws = Path(self.workspace).expanduser()
        if not ws.is_absolute():
            raise ValueError("workspace must be an absolute path")
        self.workspace = str(ws)
        return self

    def guard_eval_not_editable(self) -> None:
        """Refuse configs where the eval script itself is agent-editable."""
        from .sandbox import matches_any  # local import to avoid a cycle

        ws = Path(self.workspace)
        commands = [self.eval.command]
        if self.eval.holdout_command.strip():
            commands.append(self.eval.holdout_command)
        for command in commands:
            for token in shlex.split(command):
                candidate = (ws / token).resolve()
                try:
                    rel = candidate.relative_to(ws.resolve())
                except ValueError:
                    continue
                if candidate.is_file() and matches_any(str(rel).replace("\\", "/"), self.editable_files):
                    raise ValueError(
                        f"'{rel}' is used by an eval command and cannot be editable — "
                        "the agent must not be able to change its own grader"
                    )
        if matches_any(self.eval.metrics_file, self.editable_files):
            raise ValueError("the metrics file cannot be listed as editable")
