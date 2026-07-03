import json
import sys
import time

import pytest

from autoresearch.events import EventBus
from autoresearch.loop import ResearchLoop

from conftest import make_config

PY = sys.executable


def run_to_completion(exp, timeout=60):
    loop = ResearchLoop(exp, EventBus())
    loop.start()
    deadline = time.time() + timeout
    while loop.running and time.time() < deadline:
        time.sleep(0.05)
    assert not loop.running
    return exp


def eval_scripts(workspace):
    """Visible eval reads VALUE; holdout reads VALUE with a penalty file knob."""
    (workspace / "eval.py").write_text(
        "import json\nfrom solution import VALUE\n"
        "json.dump({'score': VALUE}, open('metrics.json', 'w'))\n")
    (workspace / "holdout.py").write_text(
        "import json\nfrom solution import VALUE\n"
        "json.dump({'score': VALUE - 0.5}, open('metrics.json', 'w'))\n")


def improving_agent():
    script = ("import re,pathlib;"
              "p=pathlib.Path('solution.py');"
              "v=int(re.search(r'VALUE = (\\d+)', p.read_text()).group(1));"
              "p.write_text(f'VALUE = {v+1}\\n');print('bumped', v+1)")
    return f'{PY} -c "{script}"'


# ---------------------------------------------------------------- holdout

def test_holdout_runs_on_champions_only(workspace, store):
    eval_scripts(workspace)
    cfg = make_config(
        workspace,
        eval={"command": f"{PY} eval.py", "metric": "score", "direction": "maximize",
              "timeout_seconds": 60, "holdout_command": f"{PY} holdout.py"},
        agent={"type": "command", "command_template": improving_agent()},
        budgets={"agent_timeout_seconds": 60, "max_iterations": 2},
    )
    exp = store.create(cfg, "")
    run_to_completion(exp)
    hist = exp.history()
    # baseline + champions carry holdout results
    assert hist[0]["holdout"]["ok"] and hist[0]["holdout"]["primary"] == 0.5
    assert hist[1]["is_champion"] and hist[1]["holdout"]["primary"] == 1.5
    assert hist[2]["holdout"]["primary"] == 2.5


def test_holdout_skipped_for_non_champions(workspace, store):
    eval_scripts(workspace)
    cfg = make_config(
        workspace,
        eval={"command": f"{PY} eval.py", "metric": "score", "direction": "maximize",
              "timeout_seconds": 60, "holdout_command": f"{PY} holdout.py"},
        agent={"type": "command", "command_template": "true"},  # no change → no improvement
        budgets={"agent_timeout_seconds": 60, "max_iterations": 1},
    )
    exp = store.create(cfg, "")
    run_to_completion(exp)
    assert exp.history()[1]["is_champion"] is False
    assert exp.history()[1]["holdout"] is None


def test_holdout_never_in_prompt(workspace, store):
    from autoresearch.prompts import build_prompt
    eval_scripts(workspace)
    cfg = make_config(
        workspace,
        eval={"command": f"{PY} eval.py", "metric": "score", "direction": "maximize",
              "timeout_seconds": 60, "holdout_command": f"{PY} holdout.py"},
    )
    history = [{"iteration": 1, "primary": 2.0, "is_champion": True, "eval_ok": True,
                "summary": "x", "holdout": {"ok": True, "primary": 1.5}}]
    prompt = build_prompt(cfg, "", 2, 2.0, {"score": 2.0}, history)
    assert "holdout" not in prompt.lower()
    assert "1.5" not in prompt


def test_holdout_script_cannot_be_editable(workspace, store):
    eval_scripts(workspace)
    cfg = make_config(
        workspace,
        editable_files=["solution.py", "holdout.py"],
        eval={"command": f"{PY} eval.py", "metric": "score", "direction": "maximize",
              "timeout_seconds": 60, "holdout_command": f"{PY} holdout.py"},
    )
    with pytest.raises(ValueError, match="grader"):
        store.create(cfg, "")


# ---------------------------------------------------------------- notebook

def test_notebook_persists_across_rejected_iterations(workspace, store):
    # agent writes a note but does NOT improve the metric
    note_writer = (f'{PY} -c "import pathlib;'
                   "pathlib.Path('AGENT_NOTES.md').write_text('tried nothing; VALUE bump next')\"")
    cfg = make_config(
        workspace,
        agent={"type": "command", "command_template": note_writer},
        budgets={"agent_timeout_seconds": 60, "max_iterations": 2},
    )
    exp = store.create(cfg, "")
    run_to_completion(exp)
    hist = exp.history()
    assert not hist[1]["is_champion"] and not hist[2]["is_champion"]
    # the notebook survived both rejected iterations
    assert "VALUE bump next" in exp.notebook()
    # notebook is not treated as an editable snapshot file or a violation
    meta = exp.iteration_meta(1)
    assert meta["sandbox"]["violations"] == []
    assert "AGENT_NOTES.md" not in json.dumps(meta["deleted_editable"])
    assert not (exp.iteration_dir(1) / "files" / "AGENT_NOTES.md").exists()


def test_notebook_reaches_prompt(workspace, store):
    from autoresearch.prompts import build_prompt
    cfg = make_config(workspace)
    prompt = build_prompt(cfg, "", 2, 1.0, {}, [], notebook="- 2-opt alone plateaus at 8.5")
    assert "2-opt alone plateaus" in prompt
    assert "AGENT_NOTES.md" in prompt
    assert "NEVER retry" in prompt


# ---------------------------------------------------------------- cost

def test_cost_budget_stop_condition(workspace, store):
    import time as _time
    cfg = make_config(
        workspace,
        budgets={"agent_timeout_seconds": 60, "max_iterations": 100, "max_cost_usd": 0.05},
    )
    exp = store.create(cfg, "")
    loop = ResearchLoop(exp, EventBus())

    # under budget → keep going
    exp.append_history({"iteration": 1, "ts": _time.time(), "eval_ok": True, "primary": 1,
                        "is_champion": True, "summary": "",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.02}})
    assert loop._should_stop(exp.config, _time.monotonic()) is False

    # over budget → finished with a cost reason
    exp.append_history({"iteration": 2, "ts": _time.time(), "eval_ok": True, "primary": 1,
                        "is_champion": False, "summary": "",
                        "usage": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.04}})
    assert loop._should_stop(exp.config, _time.monotonic()) is True
    doc = exp.load()
    assert doc["status"] == "finished" and "cost budget" in doc.get("reason", "")


def test_claude_stream_parser_captures_usage():
    from autoresearch.agents.claude_code import _StreamParser
    p = _StreamParser(None)
    p.feed(json.dumps({"type": "result", "subtype": "success", "result": "done",
                       "total_cost_usd": 0.1234,
                       "usage": {"input_tokens": 1000, "output_tokens": 500,
                                 "cache_read_input_tokens": 2000}}))
    assert p.usage == {"input_tokens": 3000, "output_tokens": 500, "cost_usd": 0.1234}
