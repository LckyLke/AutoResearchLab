import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from autoresearch.config import EnvironmentConfig
from autoresearch.envs import (EnvironmentError_, environment_prompt_block,
                               list_conda_envs, resolve_env)

from conftest import make_config


def fake_venv(tmp_path) -> Path:
    """A minimal 'venv': bin/python symlinked to the current interpreter."""
    venv = tmp_path / "fakevenv"
    (venv / "bin").mkdir(parents=True)
    os.symlink(sys.executable, venv / "bin" / "python")
    return venv


def test_system_env():
    r = resolve_env(EnvironmentConfig(type="system"))
    assert r.kind == "system" and r.overrides == {}
    assert "PATH" in r.apply()


def test_venv_resolution(tmp_path):
    venv = fake_venv(tmp_path)
    r = resolve_env(EnvironmentConfig(type="venv", venv_path=str(venv)))
    assert r.kind == "venv"
    assert r.overrides["PATH"].startswith(str(venv / "bin"))
    assert r.overrides["VIRTUAL_ENV"] == str(venv)


def test_venv_missing(tmp_path):
    with pytest.raises(EnvironmentError_, match="not a virtualenv"):
        resolve_env(EnvironmentConfig(type="venv", venv_path=str(tmp_path / "nope")))


def test_conda_missing_name():
    with pytest.raises(EnvironmentError_):
        resolve_env(EnvironmentConfig(type="conda", conda_env=""))
    with pytest.raises(EnvironmentError_, match="not found"):
        resolve_env(EnvironmentConfig(type="conda", conda_env="definitely-not-an-env-xyz"))


@pytest.mark.skipif(not list_conda_envs(), reason="no conda on this machine")
def test_conda_resolution():
    r = resolve_env(EnvironmentConfig(type="conda", conda_env=list_conda_envs()[0]["name"]))
    assert r.kind == "conda" and r.prefix
    assert r.overrides["CONDA_PREFIX"] == r.prefix


def test_prompt_block_truncates():
    from autoresearch.envs import ResolvedEnv
    r = ResolvedEnv(kind="venv", name="test", prefix="/x")
    info = {"python_version": "3.12.0",
            "packages": [f"package-{i:03d}==1.0.0" for i in range(500)]}
    block = environment_prompt_block(info, r, max_chars=400)
    assert "Python 3.12.0" in block
    assert "and" in block and "more" in block
    assert len(block) < 900


def test_loop_uses_configured_env(workspace, store, tmp_path):
    """The eval command's `python` must come from the configured venv."""
    venv = fake_venv(tmp_path)
    (workspace / "eval.py").write_text(
        "import sys, json\n"
        "open('which_python.txt', 'w').write(sys.executable)\n"
        "json.dump({'score': 1}, open('metrics.json', 'w'))\n"
    )
    cfg = make_config(
        workspace,
        eval={"command": "python eval.py", "metric": "score",
              "direction": "maximize", "timeout_seconds": 60},
        environment={"type": "venv", "venv_path": str(venv)},
        budgets={"agent_timeout_seconds": 60, "max_iterations": 1},
    )
    exp = store.create(cfg, "")
    from autoresearch.events import EventBus
    from autoresearch.loop import ResearchLoop
    loop = ResearchLoop(exp, EventBus())
    loop.start()
    deadline = time.time() + 60
    while loop.running and time.time() < deadline:
        time.sleep(0.05)
    assert exp.load()["status"] == "finished"
    used = (exp.work_dir / "which_python.txt").read_text()
    assert used == str(venv / "bin" / "python") or used == os.path.realpath(sys.executable)
    # environment introspection was cached with the experiment
    assert (exp.dir / "environment.json").exists()
