import sys

from autoresearch.config import EvalConfig
from autoresearch.evaluator import is_improvement, run_eval

PY = sys.executable


def cfg(command, **kw):
    return EvalConfig(command=command, metric="score", direction="maximize", **kw)


def test_metrics_file_parsed(tmp_path):
    (tmp_path / "e.py").write_text(
        "import json; json.dump({'score': 3.5, 'extra': 1}, open('metrics.json','w'))")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert r.ok and r.primary == 3.5 and r.metrics["extra"] == 1


def test_stdout_fallback(tmp_path):
    (tmp_path / "e.py").write_text("print('log line'); print('{\"score\": 2}')")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert r.ok and r.primary == 2.0


def test_nonzero_exit(tmp_path):
    (tmp_path / "e.py").write_text("raise SystemExit(3)")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert not r.ok and "code 3" in r.error


def test_missing_metric_key(tmp_path):
    (tmp_path / "e.py").write_text(
        "import json; json.dump({'other': 1}, open('metrics.json','w'))")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert not r.ok and "missing" in r.error


def test_non_numeric_metric(tmp_path):
    (tmp_path / "e.py").write_text(
        "import json; json.dump({'score': 'high'}, open('metrics.json','w'))")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert not r.ok and "not numeric" in r.error


def test_timeout(tmp_path):
    (tmp_path / "e.py").write_text("import time; time.sleep(5)")
    r = run_eval(cfg(f"{PY} e.py", timeout_seconds=1), tmp_path)
    assert not r.ok and "timed out" in r.error


def test_stale_metrics_removed(tmp_path):
    (tmp_path / "metrics.json").write_text('{"score": 999}')
    (tmp_path / "e.py").write_text("raise SystemExit(1)")
    r = run_eval(cfg(f"{PY} e.py"), tmp_path)
    assert not r.ok  # stale metrics must not leak into a failed run


def test_is_improvement():
    assert is_improvement(2.0, 1.0, "maximize")
    assert not is_improvement(1.0, 1.0, "maximize")
    assert is_improvement(0.5, 1.0, "minimize")
    assert not is_improvement(1.5, 1.0, "minimize")
    assert is_improvement(5.0, None, "minimize")
    assert not is_improvement(1.05, 1.0, "maximize", min_improvement=0.1)
