import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def workspace(tmp_path):
    """A tiny project: editable solution.py + protected eval.py."""
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "solution.py").write_text("VALUE = 1\n")
    (ws / "helper.py").write_text("HELP = True\n")
    (ws / "eval.py").write_text(
        "import json\n"
        "from solution import VALUE\n"
        "json.dump({'score': VALUE}, open('metrics.json', 'w'))\n"
    )
    sub = ws / "data"
    sub.mkdir()
    (sub / "notes.txt").write_text("do not touch\n")
    return ws


@pytest.fixture
def store(tmp_path):
    from autoresearch.storage import ExperimentStore
    return ExperimentStore(tmp_path / "experiments")


def make_config(ws, **overrides):
    from autoresearch.config import ExperimentConfig
    base = {
        "name": "test",
        "workspace": str(ws),
        "editable_files": ["solution.py"],
        "eval": {"command": f"{sys.executable} eval.py", "metric": "score",
                 "direction": "maximize", "timeout_seconds": 60},
        "agent": {"type": "command", "command_template": "true"},
        # inform_agent=False skips package introspection — keeps tests fast
        "environment": {"type": "system", "inform_agent": False},
        "budgets": {"agent_timeout_seconds": 60, "max_iterations": 1},
    }
    base.update(overrides)
    return ExperimentConfig.model_validate(base)
