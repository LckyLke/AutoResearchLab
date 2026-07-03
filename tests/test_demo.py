from autoresearch.demo import ensure_demo_experiment
from autoresearch.events import EventBus
from autoresearch.loop import ResearchLoop


def test_demo_seeded_once_and_stays_deleted(store):
    exp_id = ensure_demo_experiment(store)
    assert exp_id is not None
    assert any(e["id"] == exp_id for e in store.list())

    # second start: not duplicated
    assert ensure_demo_experiment(store) is None
    assert len(store.list()) == 1

    # user deletes → it must NOT come back
    store.delete(exp_id)
    assert ensure_demo_experiment(store) is None
    assert store.list() == []


def test_demo_baseline_evaluates(store):
    """The bundled TSP problem must actually run and produce its metric."""
    exp_id = ensure_demo_experiment(store)
    exp = store.get(exp_id)
    cfg = exp.config
    assert cfg.eval.metric == "tour_length" and cfg.eval.direction == "minimize"
    assert cfg.editable_files == ["solver.py"]

    # drive only the baseline phase directly — never the (real!) agent
    cfg.environment.inform_agent = False  # skip slow package introspection
    loop = ResearchLoop(exp, EventBus())
    loop._prepare_environment(cfg)
    loop._run_baseline(cfg)

    baseline = exp.history()[0]
    assert baseline["eval_ok"] is True
    assert 60 < baseline["primary"] < 70  # naive input-order tour ≈ 63.6
