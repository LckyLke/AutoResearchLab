import sys
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_config


@pytest.fixture
def client(store, monkeypatch):
    import autoresearch.server as server
    monkeypatch.setattr(server, "store", store)
    return TestClient(server.app)


def test_index_serves_gui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "AutoResearchLab" in r.text


def test_templates(client):
    r = client.get("/api/templates")
    assert r.status_code == 200
    body = r.json()
    assert "default" in body and "template" in body


def test_tree(client, workspace):
    r = client.post("/api/tree", json={"workspace": str(workspace)})
    assert r.status_code == 200
    assert "solution.py" in r.json()["files"]


def test_create_get_start_stop_delete(client, workspace):
    cfg = make_config(
        workspace,
        agent={"type": "command",
               "command_template": f"{sys.executable} -c \"print('noop')\""},
        budgets={"agent_timeout_seconds": 60, "max_iterations": 1},
    )
    r = client.post("/api/experiments",
                    json={"config": cfg.model_dump(), "instructions": "be careful"})
    assert r.status_code == 200, r.text
    exp_id = r.json()["id"]

    r = client.get(f"/api/experiments/{exp_id}")
    assert r.json()["instructions"] == "be careful"

    assert client.post(f"/api/experiments/{exp_id}/start").status_code == 200
    deadline = time.time() + 30
    while time.time() < deadline:
        doc = client.get(f"/api/experiments/{exp_id}").json()
        if not doc["loop"]["running"]:
            break
        time.sleep(0.1)
    assert doc["status"] == "finished"
    assert len(doc["history"]) == 2  # baseline + 1 iteration

    r = client.get(f"/api/experiments/{exp_id}/iterations/1")
    assert r.status_code == 200
    assert client.get(f"/api/experiments/{exp_id}/iterations/1/download").status_code == 200
    assert client.get(f"/api/experiments/{exp_id}/champion/download").status_code == 200

    assert client.delete(f"/api/experiments/{exp_id}").status_code == 200
    assert client.get(f"/api/experiments/{exp_id}").status_code == 404


def test_invalid_config_rejected(client, workspace):
    cfg = make_config(workspace).model_dump()
    cfg["editable_files"] = []
    r = client.post("/api/experiments", json={"config": cfg, "instructions": ""})
    assert r.status_code == 422  # pydantic validation
