"""Filesystem sandbox: the agent may only change whitelisted files.

Enforcement is done outside the agent, so it works with any agent type:

1. Before the agent runs, every non-editable file is hashed and made
   read-only (first line of defense).
2. After the agent runs, the tree is re-hashed. Any change outside the
   whitelist — modified, deleted, or newly created files — is reverted
   from the pristine baseline copy and reported as a violation.

Because non-editable files can never legally change, the experiment's
``baseline/`` snapshot is always a valid restore source.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path


def _norm(rel: str) -> str:
    return rel.replace("\\", "/")


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    """Glob match against workspace-relative POSIX-style paths.

    Used for the *editable* whitelist: a bare name like ``solver.py``
    matches only at the workspace root — never nested files of the same
    name (that would silently widen the whitelist)."""
    rel_path = _norm(rel_path)
    for pattern in patterns:
        pattern = _norm(pattern)
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        # allow directory patterns like "src" to cover their contents
        if rel_path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def ignore_match(rel_path: str, patterns: list[str]) -> bool:
    """Ignore-pattern matching — deliberately looser than ``matches_any``:
    a pattern without a slash (``node_modules``, ``*.pyc``, ``.venv``)
    matches that name as ANY path segment, at any depth. Patterns with a
    slash behave like ``matches_any`` (rooted glob / directory prefix)."""
    rel_path = _norm(rel_path)
    segments = rel_path.split("/")
    for pattern in patterns:
        pattern = _norm(pattern)
        if "/" in pattern:
            if fnmatch.fnmatch(rel_path, pattern) or \
                    rel_path.startswith(pattern.rstrip("/") + "/"):
                return True
        elif any(fnmatch.fnmatch(seg, pattern) for seg in segments):
            return True
    return False


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: Path, ignore_patterns: list[str]) -> list[str]:
    """All workspace-relative file paths under root, minus ignored ones."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = _norm(os.path.relpath(dirpath, root))
        if rel_dir == ".":
            rel_dir = ""
        # prune ignored directories (at any depth) so we never descend
        # into .git, node_modules, .venv, ...
        dirnames[:] = [
            d for d in dirnames
            if not ignore_match(f"{rel_dir}/{d}" if rel_dir else d, ignore_patterns)
        ]
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if not ignore_match(rel, ignore_patterns):
                out.append(rel)
    return sorted(out)


PROTECTED_PREFIXES = ("knowledge/",)  # never editable, whatever the whitelist says
SCRATCH_DIR = ".scratch"  # sanctioned agent scratch space — the prompt promises this
                          # unconditionally, so the sandbox must honor it for every
                          # experiment, including ones created with older ignore lists


def is_editable_path(rel_path: str, editable_patterns: list[str]) -> bool:
    rel_path = _norm(rel_path)
    if rel_path.startswith(PROTECTED_PREFIXES):
        return False
    return matches_any(rel_path, editable_patterns)


@dataclass
class Violation:
    path: str
    kind: str  # "modified" | "deleted" | "created"
    action: str  # what the sandbox did about it


@dataclass
class EnforcementReport:
    violations: list[Violation] = field(default_factory=list)
    changed_editable: list[str] = field(default_factory=list)
    created_editable: list[str] = field(default_factory=list)
    deleted_editable: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "violations": [vars(v) for v in self.violations],
            "changed_editable": self.changed_editable,
            "created_editable": self.created_editable,
            "deleted_editable": self.deleted_editable,
        }


class Sandbox:
    def __init__(self, work_dir: Path, baseline_dir: Path,
                 editable_patterns: list[str], ignore_patterns: list[str]):
        self.work = Path(work_dir)
        self.baseline = Path(baseline_dir)
        self.editable = editable_patterns
        # scratch space is invisible to enforcement regardless of config age;
        # sync_workdir still wipes it between iterations
        self.ignore = list(ignore_patterns) + [SCRATCH_DIR]
        self._pre_hashes: dict[str, str] = {}
        self._orig_modes: dict[str, int] = {}

    # -- lifecycle -----------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        return {rel: file_sha256(self.work / rel) for rel in walk_files(self.work, self.ignore)}

    def protect(self) -> None:
        """Hash everything and strip write permission from protected files."""
        self._pre_hashes = self.snapshot()
        self._orig_modes = {}
        for rel in self._pre_hashes:
            if not is_editable_path(rel, self.editable):
                p = self.work / rel
                mode = p.stat().st_mode
                self._orig_modes[rel] = mode
                p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

    def unprotect(self) -> None:
        for rel, mode in self._orig_modes.items():
            p = self.work / rel
            if p.exists():
                try:
                    p.chmod(mode)
                except OSError:
                    pass
        self._orig_modes = {}

    def enforce(self) -> EnforcementReport:
        """Compare against the pre-run snapshot; revert anything illegal."""
        report = EnforcementReport()
        post = self.snapshot()
        pre = self._pre_hashes

        for rel, digest in post.items():
            editable = is_editable_path(rel, self.editable)
            if rel not in pre:
                if editable:
                    report.created_editable.append(rel)
                else:
                    (self.work / rel).unlink()
                    report.violations.append(Violation(rel, "created", "deleted illegal new file"))
            elif digest != pre[rel]:
                if editable:
                    report.changed_editable.append(rel)
                else:
                    self._restore(rel)
                    report.violations.append(Violation(rel, "modified", "restored from baseline"))

        for rel in pre:
            if rel not in post:
                if is_editable_path(rel, self.editable):
                    report.deleted_editable.append(rel)
                else:
                    self._restore(rel)
                    report.violations.append(Violation(rel, "deleted", "restored from baseline"))

        self._prune_empty_dirs()
        return report

    # -- helpers -------------------------------------------------------

    def _restore(self, rel: str) -> None:
        src = self.baseline / rel
        dst = self.work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.chmod(dst.stat().st_mode | stat.S_IWUSR)
        shutil.copy2(src, dst)

    def _prune_empty_dirs(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.work, topdown=False):
            rel = _norm(os.path.relpath(dirpath, self.work))
            if rel == ".":
                continue
            if not dirnames and not filenames and not (self.baseline / rel).exists():
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass


def sync_workdir(baseline: Path, overlay_files: Path | None, work: Path,
                 ignore_patterns: list[str],
                 deleted_files: list[str] | None = None) -> None:
    """Reset ``work`` to baseline + (champion) overlay.

    ``deleted_files`` are baseline files the champion legitimately removed;
    they are excluded from the result. Copies only files that differ (by
    size+mtime) and removes files that belong to neither source, so
    consecutive iterations stay cheap.
    """
    work.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()

    def copy_tree(src_root: Path) -> None:
        for rel in walk_files(src_root, ignore_patterns):
            expected.add(rel)
            src = src_root / rel
            dst = work / rel
            if dst.exists():
                s, d = src.stat(), dst.stat()
                if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
                    continue
                dst.chmod(d.st_mode | stat.S_IWUSR)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    copy_tree(baseline)
    if overlay_files is not None and overlay_files.exists():
        copy_tree(overlay_files)
    for rel in deleted_files or []:
        expected.discard(_norm(rel))

    # remove EVERYTHING not in the expected set — including ignored junk like
    # __pycache__: stale bytecode from a previous iteration can otherwise be
    # imported instead of the champion's fresh source (same size + mtime).
    for rel in walk_files(work, []):
        if rel not in expected:
            p = work / rel
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
            p.unlink()
    for dirpath, dirnames, filenames in os.walk(work, topdown=False):
        if dirpath != str(work) and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
