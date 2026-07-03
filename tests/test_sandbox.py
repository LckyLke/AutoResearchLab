import shutil

from autoresearch.sandbox import Sandbox, matches_any, sync_workdir, walk_files

IGNORE = ["__pycache__/**", "*.pyc", ".git/**", "metrics.json"]


def make_sandbox(workspace, tmp_path):
    baseline = tmp_path / "baseline"
    shutil.copytree(workspace, baseline)
    return Sandbox(workspace, baseline, ["solution.py"], IGNORE)


def test_matches_any_globs():
    assert matches_any("src/models/a.py", ["src/models/*.py"])
    assert matches_any("a/b/c.txt", ["a"])  # directory pattern covers children
    assert not matches_any("eval.py", ["solution.py"])


def test_ignore_match_hits_nested_junk():
    from autoresearch.sandbox import ignore_match
    # slash-less patterns match the name as ANY path segment
    assert ignore_match("node_modules/x.js", ["node_modules"])
    assert ignore_match("frontend/node_modules/react/index.js", ["node_modules"])
    assert ignore_match("a/b/__pycache__/m.pyc", ["__pycache__"])
    assert ignore_match("deep/dir/file.pyc", ["*.pyc"])
    assert not ignore_match("frontend/app/page.tsx", ["node_modules"])
    # rooted patterns (with slash) stay rooted
    assert ignore_match("build/out.o", ["build/**"])
    assert not ignore_match("src/build/out.o", ["build/**"])


def test_editable_whitelist_is_not_segment_matched():
    # a bare editable name must NOT whitelist same-named files in subdirs
    assert matches_any("solution.py", ["solution.py"])
    assert not matches_any("lib/solution.py", ["solution.py"])


def test_walk_files_prunes_nested_junk(tmp_path):
    from autoresearch.config import DEFAULT_IGNORE_PATTERNS
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x")
    nm = tmp_path / "frontend" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("x")
    (tmp_path / "frontend" / "page.tsx").parent.mkdir(exist_ok=True)
    (tmp_path / "frontend" / "page.tsx").write_text("x")
    files = walk_files(tmp_path, DEFAULT_IGNORE_PATTERNS)
    assert files == ["app/main.py", "frontend/page.tsx"]


def test_editable_change_is_kept(workspace, tmp_path):
    sb = make_sandbox(workspace, tmp_path)
    sb.protect()
    (workspace / "solution.py").write_text("VALUE = 2\n")
    sb.unprotect()
    report = sb.enforce()
    assert report.ok
    assert report.changed_editable == ["solution.py"]
    assert (workspace / "solution.py").read_text() == "VALUE = 2\n"


def test_illegal_edit_is_reverted(workspace, tmp_path):
    sb = make_sandbox(workspace, tmp_path)
    sb.protect()
    target = workspace / "helper.py"
    target.chmod(0o644)  # simulate an agent forcing permissions
    target.write_text("HELP = False\n")
    sb.unprotect()
    report = sb.enforce()
    assert not report.ok
    assert report.violations[0].path == "helper.py"
    assert (workspace / "helper.py").read_text() == "HELP = True\n"


def test_illegal_new_file_is_deleted(workspace, tmp_path):
    sb = make_sandbox(workspace, tmp_path)
    sb.protect()
    (workspace / "sneaky.py").write_text("import os\n")
    sb.unprotect()
    report = sb.enforce()
    assert [v.kind for v in report.violations] == ["created"]
    assert not (workspace / "sneaky.py").exists()


def test_deleted_protected_file_is_restored(workspace, tmp_path):
    sb = make_sandbox(workspace, tmp_path)
    sb.protect()
    f = workspace / "data" / "notes.txt"
    f.chmod(0o644)
    f.unlink()
    sb.unprotect()
    report = sb.enforce()
    assert not report.ok
    assert (workspace / "data" / "notes.txt").read_text() == "do not touch\n"


def test_scratch_dir_never_a_violation_even_with_old_ignore_list(workspace, tmp_path):
    # experiments created before .scratch entered the defaults must still honor it
    sb = make_sandbox(workspace, tmp_path)  # IGNORE here has no ".scratch"
    sb.protect()
    scratch = workspace / ".scratch"
    scratch.mkdir()
    (scratch / "probe.py").write_text("print('temporary')")
    sb.unprotect()
    report = sb.enforce()
    assert report.ok, report.to_dict()
    assert (scratch / "probe.py").exists()  # left alone by enforcement
    # ...but the next workdir sync wipes it
    baseline = tmp_path / "baseline"
    sync_workdir(baseline, None, workspace, IGNORE)
    assert not scratch.exists()


def test_protect_makes_files_readonly(workspace, tmp_path):
    sb = make_sandbox(workspace, tmp_path)
    sb.protect()
    try:
        assert not (workspace / "eval.py").stat().st_mode & 0o200
        assert (workspace / "solution.py").stat().st_mode & 0o200
    finally:
        sb.unprotect()
    assert (workspace / "eval.py").stat().st_mode & 0o200


def test_sync_workdir_overlay_and_cleanup(workspace, tmp_path):
    baseline = tmp_path / "baseline"
    shutil.copytree(workspace, baseline)
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "solution.py").write_text("VALUE = 99\n")
    work = tmp_path / "work"

    sync_workdir(baseline, overlay, work, IGNORE)
    assert (work / "solution.py").read_text() == "VALUE = 99\n"
    assert (work / "eval.py").exists()

    # junk from a previous run is removed on the next sync
    (work / "junk.txt").write_text("x")
    sync_workdir(baseline, overlay, work, IGNORE)
    assert not (work / "junk.txt").exists()

    # deleted_files excludes a file the champion removed
    sync_workdir(baseline, overlay, work, IGNORE, deleted_files=["helper.py"])
    assert not (work / "helper.py").exists()
    assert "helper.py" not in walk_files(work, IGNORE)
