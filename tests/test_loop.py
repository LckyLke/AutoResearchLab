"""Integration: full loop with a scripted 'agent' (no model calls)."""

import json
import sys
import time
import zipfile
from io import BytesIO

import pytest

from autoresearch.events import EventBus
from autoresearch.loop import ResearchLoop

from conftest import make_config

PY = sys.executable


def run_loop_until_done(exp, timeout=60):
    bus = EventBus()
    loop = ResearchLoop(exp, bus)
    loop.start()
    deadline = time.time() + timeout
    while loop.running and time.time() < deadline:
        time.sleep(0.05)
    assert not loop.running, "loop did not finish in time"
    return exp


def improving_agent_cmd():
    # each run bumps VALUE by 1 → strictly improving metric
    script = (
        "import re,pathlib;"
        "p=pathlib.Path('solution.py');"
        "v=int(re.search(r'VALUE = (\\d+)', p.read_text()).group(1));"
        "p.write_text(f'VALUE = {v+1}\\n');"
        "print('bumped VALUE to', v+1)"
    )
    return f'{PY} -c "{script}"'


def test_champion_progression(workspace, store):
    cfg = make_config(
        workspace,
        agent={"type": "command", "command_template": improving_agent_cmd()},
        budgets={"agent_timeout_seconds": 60, "max_iterations": 3},
    )
    exp = store.create(cfg, "improve the value")
    run_loop_until_done(exp)

    history = exp.history()
    assert [h["iteration"] for h in history] == [0, 1, 2, 3]
    assert history[0]["primary"] == 1  # baseline
    assert history[3]["primary"] == 4
    assert all(h["is_champion"] for h in history)
    assert exp.champion_meta()["iteration"] == 3
    assert exp.load()["status"] == "finished"

    # champion zip contains the improved file and the protected files
    data = exp.build_version_zip(None)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert "VALUE = 4" in zf.read("solution.py").decode()
        assert "do not touch" in zf.read("data/notes.txt").decode()


def test_bad_change_not_promoted(workspace, store):
    # agent breaks the file → eval fails → champion stays at baseline
    cfg = make_config(
        workspace,
        agent={"type": "command",
               "command_template": f'{PY} -c "open(\'solution.py\',\'w\').write(\'oops(\')"'},
    )
    exp = store.create(cfg, "")
    run_loop_until_done(exp)
    history = exp.history()
    assert history[1]["eval_ok"] is False
    assert history[1]["is_champion"] is False
    assert exp.champion_meta()["iteration"] == 0


def test_violations_are_reverted_and_logged(workspace, store):
    # agent tries to cheat by rewriting the grader
    cheat = (
        "import pathlib,os;"
        "p=pathlib.Path('eval.py'); os.chmod(p, 0o644);"
        "p.write_text('import json; json.dump({\\'score\\': 9999}, open(\\'metrics.json\\',\\'w\\'))')"
    )
    cfg = make_config(
        workspace,
        agent={"type": "command", "command_template": f'{PY} -c "{cheat}"'},
    )
    exp = store.create(cfg, "")
    run_loop_until_done(exp)
    meta = exp.iteration_meta(1)
    assert meta["sandbox"]["violations"], "cheating must be recorded"
    assert meta["primary"] == 1  # original eval ran, not the forged one
    assert meta["is_champion"] is False  # no improvement


def test_eval_file_cannot_be_editable(workspace, store):
    cfg = make_config(workspace, editable_files=["solution.py", "eval.py"])
    with pytest.raises(ValueError, match="grader"):
        store.create(cfg, "")


def test_original_workspace_untouched(workspace, store):
    before = (workspace / "solution.py").read_text()
    cfg = make_config(
        workspace,
        agent={"type": "command", "command_template": improving_agent_cmd()},
    )
    exp = store.create(cfg, "")
    run_loop_until_done(exp)
    assert (workspace / "solution.py").read_text() == before
    assert exp.history()[1]["primary"] == 2  # improvement happened in the copy
