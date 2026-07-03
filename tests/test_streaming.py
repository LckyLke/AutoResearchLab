"""Live-activity streaming from agents."""

import json
import sys
from pathlib import Path

from autoresearch.agents.claude_code import _StreamParser
from autoresearch.agents.command import CommandAgent
from autoresearch.config import AgentConfig


def test_command_agent_streams_lines(tmp_path):
    seen = []
    cfg = AgentConfig(type="command",
                      command_template=f"{sys.executable} -c \"print('one'); print('two')\"")
    result = CommandAgent(cfg).run("task", tmp_path, 30, on_activity=seen.append)
    assert result.ok
    assert seen == ["one", "two"]


def test_stream_parser_extracts_activity():
    seen = []
    p = _StreamParser(seen.append)
    p.feed(json.dumps({"type": "system", "subtype": "init", "model": "claude-x"}))
    p.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me look at the file."},
        {"type": "tool_use", "name": "Edit",
         "input": {"file_path": "predict.py", "old_string": "a", "new_string": "b"}},
    ]}}))
    p.feed(json.dumps({"type": "result", "subtype": "success",
                       "result": "Done: improved the model."}))
    assert any("claude-x" in s for s in seen)
    assert any(s.startswith("⚙ Edit predict.py") for s in seen)
    assert p.final_result == "Done: improved the model."
    # non-JSON lines must not crash the parser
    p.feed("plain text noise")
    assert "plain text noise" in p.log_lines


def test_stream_parser_survives_broken_observer():
    import os

    def bad(_line):
        raise RuntimeError("observer bug")

    from autoresearch.agents.proc import run_streaming
    r = run_streaming([sys.executable, "-c", "print('x')"], Path("."), 30,
                      env=dict(os.environ), on_line=bad)
    assert r.returncode == 0 and r.lines == ["x"]
