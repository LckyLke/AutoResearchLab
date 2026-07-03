"""Seeds the bundled demo experiment on first server start.

A marker file in the data directory records that seeding happened, so a
user who deletes the demo is respected — it never comes back on its own.
"""

from __future__ import annotations

from pathlib import Path

from .config import AgentConfig, BudgetConfig, EvalConfig, ExperimentConfig
from .storage import ExperimentStore

ROOT = Path(__file__).resolve().parent.parent
DEMO_WORKSPACE = ROOT / "examples" / "tsp"
MARKER_NAME = ".demo-seeded"

DEMO_INSTRUCTIONS = """\
# Instructions: shortest tour through 120 cities (TSP)

`solver.py` must implement `solve(cities) -> list[int]`: an ordering of ALL
city indices (a permutation). The tour is closed — the last city connects
back to the first. `eval.py` scores the total tour length (lower is better)
on a fixed, deterministic map.

## Constraints
- Python standard library only — no third-party imports.
- The evaluation enforces a **15 second** compute budget for `solve()`.
  Leave safety margin; a solver that overruns fails the iteration.
- Keep the `solve(cities)` signature exactly as it is.

## Ideas worth exploring (roughly in order of power)
- Nearest-neighbour construction instead of input order
- 2-opt local search (uncross edges) until no improvement or time runs out
- Or-opt (relocate short segments), greedy-edge construction
- Simulated annealing / random restarts with the remaining time budget

## Things to avoid
- Brute force or O(n³) loops without a time check — the budget will kill it
- Hard-coding a tour — the grader validates against the real city set
"""


def demo_config() -> ExperimentConfig:
    return ExperimentConfig(
        name="demo · traveling salesman",
        workspace=str(DEMO_WORKSPACE),
        editable_files=["solver.py"],
        eval=EvalConfig(command="python eval.py", metric="tour_length",
                        direction="minimize", timeout_seconds=120),
        agent=AgentConfig(type="claude_code"),
        budgets=BudgetConfig(agent_timeout_seconds=900),
    )


def ensure_demo_experiment(store: ExperimentStore) -> str | None:
    """Create the demo once per data directory. Returns the new id or None."""
    marker = store.root / MARKER_NAME
    if marker.exists() or not DEMO_WORKSPACE.is_dir():
        return None
    exp = store.create(demo_config(), DEMO_INSTRUCTIONS)
    marker.write_text(exp.id + "\n")
    return exp.id
