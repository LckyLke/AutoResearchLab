"""Python environment support (conda / venv / system).

Rather than shell-activating environments, we resolve the environment's
prefix and prepend its ``bin`` directory to PATH — fast, shell-free, and
works identically for the eval command, the agent subprocess, and the
API agents' ``run_command`` tool.

The resolved environment is also introspected (python version + installed
packages) so the agent can be told exactly what it has to work with.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import EnvironmentConfig


class EnvironmentError_(Exception):
    """Raised when the configured environment cannot be resolved."""


@dataclass
class ResolvedEnv:
    kind: str                     # system | conda | venv
    name: str                     # display name
    prefix: str | None            # env root, None for system
    overrides: dict[str, str] = field(default_factory=dict)  # env vars to apply

    def apply(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env.update(self.overrides)
        return env

    def describe(self) -> str:
        if self.kind == "system":
            return "the system Python environment"
        return f"the {self.kind} environment '{self.name}'"


def _conda_binary() -> str | None:
    for candidate in ("conda", "mamba", "micromamba"):
        path = shutil.which(candidate)
        if path:
            return path
    for guess in ("~/miniconda3/bin/conda", "~/anaconda3/bin/conda",
                  "~/miniforge3/bin/conda", "/opt/conda/bin/conda"):
        p = Path(guess).expanduser()
        if p.exists():
            return str(p)
    return None


def list_conda_envs() -> list[dict]:
    """[{name, prefix}] for every conda env on this machine (empty if no conda)."""
    conda = _conda_binary()
    if not conda:
        return []
    try:
        out = subprocess.run([conda, "env", "list", "--json"], capture_output=True,
                             text=True, timeout=30)
        envs = json.loads(out.stdout).get("envs", [])
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return []
    result = []
    for prefix in envs:
        p = Path(prefix)
        name = "base" if (p / "envs").exists() or p.name in ("miniconda3", "anaconda3", "miniforge3") \
            else p.name
        result.append({"name": name, "prefix": str(p)})
    return result


def _bin_dir(prefix: Path) -> Path:
    return prefix / ("Scripts" if os.name == "nt" else "bin")


def resolve_env(cfg: EnvironmentConfig) -> ResolvedEnv:
    if cfg.type == "system":
        return ResolvedEnv(kind="system", name="system", prefix=None)

    if cfg.type == "conda":
        spec = cfg.conda_env.strip()
        if not spec:
            raise EnvironmentError_("conda environment name (or path) is required")
        prefix = Path(spec).expanduser()
        if not (prefix.is_absolute() and prefix.is_dir()):
            match = next((e for e in list_conda_envs() if e["name"] == spec), None)
            if match is None:
                raise EnvironmentError_(f"conda environment not found: {spec!r}")
            prefix = Path(match["prefix"])
        if not (_bin_dir(prefix) / "python").exists() and not (prefix / "python.exe").exists():
            raise EnvironmentError_(f"no python inside conda env at {prefix}")
        return ResolvedEnv(
            kind="conda", name=spec, prefix=str(prefix),
            overrides={
                "PATH": f"{_bin_dir(prefix)}{os.pathsep}{os.environ.get('PATH', '')}",
                "CONDA_PREFIX": str(prefix),
                "CONDA_DEFAULT_ENV": prefix.name,
            },
        )

    if cfg.type == "venv":
        spec = cfg.venv_path.strip()
        if not spec:
            raise EnvironmentError_("venv path is required")
        prefix = Path(spec).expanduser().resolve()
        if not (_bin_dir(prefix) / "python").exists() and not (prefix / "Scripts" / "python.exe").exists():
            raise EnvironmentError_(f"not a virtualenv (no python found): {prefix}")
        return ResolvedEnv(
            kind="venv", name=prefix.name, prefix=str(prefix),
            overrides={
                "PATH": f"{_bin_dir(prefix)}{os.pathsep}{os.environ.get('PATH', '')}",
                "VIRTUAL_ENV": str(prefix),
            },
        )

    raise EnvironmentError_(f"unknown environment type: {cfg.type}")


def introspect_env(resolved: ResolvedEnv, timeout: int = 90) -> dict:
    """Python version + installed packages of the resolved environment."""
    env = resolved.apply()
    info: dict = {"kind": resolved.kind, "name": resolved.name,
                  "prefix": resolved.prefix, "python_version": None, "packages": []}
    try:
        v = subprocess.run(["python", "-c", "import sys; print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=timeout, env=env)
        if v.returncode == 0:
            info["python_version"] = v.stdout.strip()
        pip = subprocess.run(["python", "-m", "pip", "list", "--format=json",
                              "--disable-pip-version-check"],
                             capture_output=True, text=True, timeout=timeout, env=env)
        if pip.returncode == 0:
            pkgs = json.loads(pip.stdout)
            info["packages"] = sorted(f"{p['name']}=={p['version']}" for p in pkgs)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        info["error"] = str(exc)
    return info


def environment_prompt_block(info: dict, resolved: ResolvedEnv, max_chars: int = 3500) -> str:
    """The '## Environment' section injected into the agent prompt."""
    lines = [f"All commands run inside {resolved.describe()}"]
    if info.get("python_version"):
        lines[0] += f" (Python {info['python_version']})"
    lines[0] += ". `python` and `pip` on PATH belong to this environment."
    pkgs = info.get("packages") or []
    if pkgs:
        listing = ", ".join(pkgs)
        if len(listing) > max_chars:
            shown = []
            used = 0
            for p in pkgs:
                if used + len(p) + 2 > max_chars:
                    break
                shown.append(p)
                used += len(p) + 2
            listing = ", ".join(shown) + f", … and {len(pkgs) - len(shown)} more"
        lines.append(f"Installed packages ({len(pkgs)}): {listing}")
        lines.append("Rely on these packages; do not install anything new unless the "
                     "user instructions explicitly allow it.")
    return "\n".join(lines)
