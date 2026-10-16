"""Where the command-line tools are, when PATH cannot be trusted.

The desktop app does not start with the PATH of a login shell, so a tool
installed through a version manager (`mise`) or into `~/.local/bin` is not
found by name. This looks on PATH first, then in the usual places, then asks
the version manager, then reads its install folders. A version manager's shim
(a stand-in that needs the manager and a project setting to work) is never
returned: the real binary is.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
KNOWN_DIRS = ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "~/.local/bin", "~/.bun/bin",
              "~/.npm-global/bin", "~/.cargo/bin", "~/go/bin"]

_cache: dict[str, str] = {}


@dataclass
class Lookup:
    path: str | None
    searched: list[str] = field(default_factory=list)

    def missing(self, name: str) -> str:
        return f"{name} was not found (looked on PATH, in {', '.join(KNOWN_DIRS)}, `mise which` and {_mise_data()}/installs)"


def _mise_data() -> Path:
    return Path(os.environ.get("MISE_DATA_DIR") or HOME / ".local" / "share" / "mise").expanduser()


def _is_shim(p: Path) -> bool:
    try:
        return p.parent.name == "shims" or p.resolve().name == "mise"
    except OSError:
        return True


def _runnable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK) and not _is_shim(p)


def _version_key(p: Path) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", p.name)]


def _from_dirs(name: str, dirs: list[str]) -> str | None:
    for d in dirs:
        p = Path(d).expanduser() / name
        if _runnable(p):
            return str(p)
    return None


def _from_mise(name: str, searched: list[str]) -> str | None:
    mise = _from_dirs("mise", os.environ.get("PATH", "").split(os.pathsep) + KNOWN_DIRS)
    if mise:
        searched.append(f"{mise} which {name}")
        try:
            r = subprocess.run([mise, "which", name], capture_output=True, text=True, timeout=10,
                               cwd=str(HOME), env={**os.environ, "HOME": str(HOME)})
            out = r.stdout.strip()
            if r.returncode == 0 and out and _runnable(Path(out)):
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
    root = _mise_data() / "installs" / name
    searched.append(f"{root}/*")
    if not root.is_dir():
        return None
    versions = sorted({v.resolve() for v in root.iterdir() if v.is_dir()}, key=_version_key, reverse=True)
    for v in versions:
        for rel in (name, f"bin/{name}", *(f"{sub.name}/bin/{name}" for sub in sorted(v.glob("*/")))):
            if _runnable(v / rel):
                return str(v / rel)
    return None


def find(name: str) -> Lookup:
    """The real executable for `name`, or None with where it looked."""
    hit = _cache.get(name)
    if hit and _runnable(Path(hit)):
        return Lookup(hit)
    searched = ["PATH", *KNOWN_DIRS]
    found = _from_dirs(name, os.environ.get("PATH", "").split(os.pathsep) + KNOWN_DIRS) or _from_mise(name, searched)
    if found:
        _cache[name] = found
    return Lookup(found, searched)


def which(name: str) -> str | None:
    return find(name).path
