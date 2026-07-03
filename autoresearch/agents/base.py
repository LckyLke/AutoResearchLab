from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# observer for live progress: receives short human-readable activity lines
OnActivity = Callable[[str], None]


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


@dataclass
class AgentResult:
    ok: bool
    summary: str = ""  # the agent's final message — stored and fed to later iterations
    log: str = ""      # full transcript for the user
    error: str | None = None
    # {"input_tokens": int, "output_tokens": int, "cost_usd": float|None}
    usage: dict | None = None


class Agent(ABC):
    """One iteration = one ``run`` call. Implementations must be
    non-interactive, respect the timeout, run subprocesses with ``env``
    (the resolved Python environment), and report progress via
    ``on_activity`` when they can."""

    @abstractmethod
    def run(self, prompt: str, workdir: Path, timeout_seconds: int,
            env: dict[str, str] | None = None,
            on_activity: OnActivity | None = None) -> AgentResult:
        ...
