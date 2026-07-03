"""Builds the per-iteration prompt handed to the agent.

The prompt combines: the standing rules of the system, the user's
instructions file, the eval contract, and a compact history of previous
attempts so the agent does not repeat failed ideas.
"""

from __future__ import annotations

from .config import ExperimentConfig

MAX_HISTORY_IN_PROMPT = 8
MAX_NOTEBOOK_CHARS = 8000  # tail of the notebook included in the prompt
NOTEBOOK_FILE = "AGENT_NOTES.md"

NOTEBOOK_SEED = """\
# Research notebook

(Nothing recorded yet — this is iteration 1.)
"""


def _fmt_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def build_prompt(cfg: ExperimentConfig, instructions: str, iteration: int,
                 champion_primary: float | None, champion_metrics: dict,
                 history: list[dict], environment_block: str = "",
                 knowledge_block: str = "", notebook: str = "") -> str:
    goal = "maximize" if cfg.eval.direction == "maximize" else "minimize"
    editable = "\n".join(f"- {p}" for p in cfg.editable_files)

    attempts = [h for h in history if h.get("iteration", 0) > 0]
    recent = attempts[-MAX_HISTORY_IN_PROMPT:]
    if recent:
        lines = []
        for h in recent:
            score = _fmt_metric(h.get("primary"))
            tag = "CHAMPION" if h.get("is_champion") else ("failed eval" if not h.get("eval_ok") else "no improvement")
            summary = (h.get("summary") or "").strip().replace("\n", " ")[:300]
            lines.append(f"- iteration {h['iteration']}: {cfg.eval.metric}={score} [{tag}] {summary}")
        history_block = "\n".join(lines)
    else:
        history_block = "- none yet; this is the first attempt"

    other = {k: v for k, v in champion_metrics.items() if k != cfg.eval.metric}
    other_block = f"\nOther metrics of the champion: {other}" if other else ""
    env_section = f"\n## Environment\n{environment_block}\n" if environment_block else ""
    knowledge_section = f"\n## Knowledge library\n{knowledge_block}\n" if knowledge_block else ""

    nb = (notebook or NOTEBOOK_SEED).strip()
    if len(nb) > MAX_NOTEBOOK_CHARS:
        nb = "…(older entries truncated)\n" + nb[-MAX_NOTEBOOK_CHARS:]
    notebook_section = f"""
## Your research notebook (`{NOTEBOOK_FILE}`)
This file is YOUR persistent memory. It survives every iteration — including
rejected ones — so keep it current:
- Before choosing an approach, check the notebook: NEVER retry an idea that is
  recorded as failed unless you change it in a meaningful, stated way.
- Before finishing, update `{NOTEBOOK_FILE}`: what you tried this iteration, the
  reasoning, and what to try or avoid next. Keep it organized and concise
  (prune stale entries); it is capped at ~{MAX_NOTEBOOK_CHARS} characters in your briefing.
Current contents:
<notebook>
{nb}
</notebook>
"""

    return f"""You are an autonomous research engineer improving a codebase against a fixed evaluation.

## Objective
{goal.upper()} the metric `{cfg.eval.metric}`.
Current champion (best so far): {cfg.eval.metric} = {_fmt_metric(champion_primary)}{other_block}
This is iteration {iteration}. Your change is kept ONLY if it strictly improves the champion metric.

## Rules
- You may ONLY create or modify these files (glob patterns, relative to the working directory):
{editable}
- Every other file is read-only. Changes elsewhere are automatically reverted and waste the iteration.
- For temporary files (probes, logs, experiments) use the `.scratch/` directory — anything
  there is permitted and wiped after the iteration. Stray files created anywhere else are deleted.
- Do NOT modify the evaluation code or the metrics file — it is your grader.
- The evaluation is run for you afterwards with: `{cfg.eval.command}`
  It writes `{cfg.eval.metrics_file}`; the primary metric key is `{cfg.eval.metric}`.
  You may run this command yourself to check your work before finishing.
- Make ONE focused, well-reasoned improvement per iteration. Prefer ideas not tried before.
{env_section}{knowledge_section}{notebook_section}
## Recent attempts (do not repeat failures)
{history_block}

## User instructions
{instructions.strip() or "(none provided)"}

## Deliverable
Apply your change to the editable files, then end with a short plain-text summary (3-6 sentences):
what you changed, why you expect it to improve `{cfg.eval.metric}`, and what to try next if it fails.
"""
